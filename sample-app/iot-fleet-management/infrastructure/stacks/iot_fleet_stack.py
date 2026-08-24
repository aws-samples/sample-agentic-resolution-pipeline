"""IoT Fleet Management - CDK Stack.

Deploys 4 microservices on ECS Fargate behind an ALB, with DynamoDB tables,
ElastiCache Redis, CloudWatch alarms, X-Ray tracing, and SNS notifications.
"""

import os
from constructs import Construct
import aws_cdk as cdk
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecr_assets as ecr_assets,
    aws_elasticloadbalancingv2 as elbv2,
    aws_dynamodb as dynamodb,
    aws_elasticache as elasticache,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cw_actions,
    aws_sns as sns,
    aws_logs as logs,
    aws_iam as iam,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
)


class IoTFleetStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Path to services directory (relative to infrastructure/)
        services_path = os.path.join(os.path.dirname(__file__), "..", "..", "services")

        # ---------------------------------------------------------------
        # VPC - lookup existing if vpc_id context is set, otherwise create new
        # Usage:
        #   Existing VPC: cdk deploy -c vpc_id=vpc-xxxxxxxxx
        #   New VPC:      cdk deploy (no context)
        # ---------------------------------------------------------------
        vpc_id = self.node.try_get_context("vpc_id")
        if vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "IoTFleetVpc", vpc_id=vpc_id)
        else:
            vpc = ec2.Vpc(
                self,
                "IoTFleetVpc",
                max_azs=2,
                nat_gateways=1,
                subnet_configuration=[
                    ec2.SubnetConfiguration(
                        name="Public",
                        subnet_type=ec2.SubnetType.PUBLIC,
                        cidr_mask=24,
                    ),
                    ec2.SubnetConfiguration(
                        name="Private",
                        subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                        cidr_mask=24,
                    ),
                ],
            )

        # ---------------------------------------------------------------
        # SNS Topic for alert notifications
        # ---------------------------------------------------------------
        alert_topic = sns.Topic(
            self,
            "AlertNotificationsTopic",
            topic_name="iot-fleet-alerts",
            display_name="IoT Fleet Alert Notifications",
        )

        # ---------------------------------------------------------------
        # DynamoDB Tables
        # ---------------------------------------------------------------
        telemetry_table = dynamodb.Table(
            self,
            "TelemetryTable",
            table_name="iot-fleet-telemetry",
            partition_key=dynamodb.Attribute(
                name="device_id", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="timestamp", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        firmware_table = dynamodb.Table(
            self,
            "FirmwareTable",
            table_name="iot-fleet-firmware",
            partition_key=dynamodb.Attribute(
                name="device_type", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        geofence_table = dynamodb.Table(
            self,
            "GeofenceTable",
            table_name="iot-fleet-geofences",
            partition_key=dynamodb.Attribute(
                name="zone_id", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ---------------------------------------------------------------
        # ElastiCache Redis (single node, cache.t3.micro)
        # ---------------------------------------------------------------
        redis_sg = ec2.SecurityGroup(
            self,
            "RedisSG",
            vpc=vpc,
            description="Security group for ElastiCache Redis",
            allow_all_outbound=True,
        )
        redis_sg.add_ingress_rule(
            ec2.Peer.ipv4(vpc.vpc_cidr_block),
            ec2.Port.tcp(6379),
            "Allow Redis access from VPC",
        )

        redis_subnet_group = elasticache.CfnSubnetGroup(
            self,
            "RedisSubnetGroup",
            description="Subnet group for IoT Fleet Redis",
            subnet_ids=[
                subnet.subnet_id
                for subnet in vpc.select_subnets(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ).subnets
            ],
            cache_subnet_group_name="iot-fleet-redis-subnet-group",
        )

        redis_cluster = elasticache.CfnCacheCluster(
            self,
            "RedisCluster",
            engine="redis",
            cache_node_type="cache.t3.micro",
            num_cache_nodes=1,
            cluster_name="iot-fleet-redis",
            vpc_security_group_ids=[redis_sg.security_group_id],
            cache_subnet_group_name=redis_subnet_group.cache_subnet_group_name,
        )
        redis_cluster.node.add_dependency(redis_subnet_group)

        # Redis endpoint (will resolve at deploy time)
        redis_host = redis_cluster.attr_redis_endpoint_address

        # ---------------------------------------------------------------
        # ECS Cluster with Container Insights
        # ---------------------------------------------------------------
        cluster = ecs.Cluster(
            self,
            "IoTFleetCluster",
            vpc=vpc,
            cluster_name="iot-fleet-cluster",
            container_insights_v2=ecs.ContainerInsights.ENABLED,
        )

        # ---------------------------------------------------------------
        # ALB
        # ---------------------------------------------------------------
        alb_sg = ec2.SecurityGroup(
            self,
            "AlbSG",
            vpc=vpc,
            description="Security group for ALB",
            allow_all_outbound=True,
        )
        alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "Allow HTTP"
        )

        alb = elbv2.ApplicationLoadBalancer(
            self,
            "IoTFleetALB",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
            load_balancer_name="iot-fleet-alb",
        )

        listener = alb.add_listener(
            "HttpListener",
            port=80,
            default_action=elbv2.ListenerAction.fixed_response(
                status_code=404,
                content_type="text/plain",
                message_body="Not Found",
            ),
        )

        # ---------------------------------------------------------------
        # Security group for ECS tasks
        # ---------------------------------------------------------------
        ecs_sg = ec2.SecurityGroup(
            self,
            "EcsTasksSG",
            vpc=vpc,
            description="Security group for ECS Fargate tasks",
            allow_all_outbound=True,
        )
        ecs_sg.add_ingress_rule(
            alb_sg, ec2.Port.tcp_range(8080, 8083), "Allow ALB to ECS"
        )

        # ---------------------------------------------------------------
        # CloudWatch Log Groups
        # ---------------------------------------------------------------
        log_groups = {}
        for svc_name in [
            "telemetry-ingest",
            "alert-engine",
            "firmware-service",
            "geofence-service",
        ]:
            log_groups[svc_name] = logs.LogGroup(
                self,
                f"LogGroup-{svc_name}",
                log_group_name=f"/iot-fleet/{svc_name}",
                retention=logs.RetentionDays.TWO_WEEKS,
                removal_policy=RemovalPolicy.DESTROY,
            )

        # ---------------------------------------------------------------
        # Docker Image Assets
        # ---------------------------------------------------------------
        images = {}
        for svc_name in [
            "telemetry-ingest",
            "alert-engine",
            "firmware-service",
            "geofence-service",
        ]:
            images[svc_name] = ecr_assets.DockerImageAsset(
                self,
                f"Image-{svc_name}",
                directory=os.path.join(services_path, svc_name),
                platform=ecr_assets.Platform.LINUX_AMD64,
            )

        # ---------------------------------------------------------------
        # IAM Task Role (shared base, per-service grants added below)
        # ---------------------------------------------------------------
        def create_task_role(svc_name: str) -> iam.Role:
            role = iam.Role(
                self,
                f"TaskRole-{svc_name}",
                assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
                role_name=f"iot-fleet-{svc_name}-task-role-{self.region}",
            )
            # X-Ray write access
            role.add_managed_policy(
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "AWSXRayDaemonWriteAccess"
                )
            )
            # CloudWatch metrics
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=[
                        "cloudwatch:PutMetricData",
                        "cloudwatch:GetMetricData",
                    ],
                    resources=["*"],
                )
            )
            # SNS publish
            alert_topic.grant_publish(role)
            return role

        # ---------------------------------------------------------------
        # Service definitions
        # ---------------------------------------------------------------
        service_configs = {
            "telemetry-ingest": {
                "port": 8080,
                "path_pattern": "/telemetry/*",
                "priority": 1,
                "table": telemetry_table,
                "env_extras": {},
            },
            "alert-engine": {
                "port": 8081,
                "path_pattern": "/alerts/*",
                "priority": 2,
                "table": telemetry_table,  # reads telemetry for alerting
                "env_extras": {
                    "REDIS_HOST": redis_host,
                    "ALERT_ENGINE_URL": "http://localhost:8081",
                },
            },
            "firmware-service": {
                "port": 8082,
                "path_pattern": "/firmware/*",
                "priority": 3,
                "table": firmware_table,
                "env_extras": {},
            },
            "geofence-service": {
                "port": 8083,
                "path_pattern": "/geofence/*",
                "priority": 4,
                "table": geofence_table,
                "env_extras": {},
            },
        }

        target_groups = {}
        fargate_services = {}

        for svc_name, config in service_configs.items():
            task_role = create_task_role(svc_name)

            # Grant DynamoDB access
            config["table"].grant_read_write_data(task_role)

            # Task Definition
            task_def = ecs.FargateTaskDefinition(
                self,
                f"TaskDef-{svc_name}",
                cpu=512,
                memory_limit_mib=1024,
                task_role=task_role,
                family=f"iot-fleet-{svc_name}",
            )

            # Environment variables
            env_vars = {
                "AWS_DEFAULT_REGION": "us-east-1",
                "TABLE_NAME": config["table"].table_name,
                "ALERT_SNS_TOPIC_ARN": alert_topic.topic_arn,
                "SERVICE_NAME": svc_name,
                "AWS_XRAY_TRACING_NAME": svc_name,
            }
            env_vars.update(config["env_extras"])

            # Main container
            container = task_def.add_container(
                f"Container-{svc_name}",
                image=ecs.ContainerImage.from_docker_image_asset(images[svc_name]),
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix=svc_name,
                    log_group=log_groups[svc_name],
                ),
                environment=env_vars,
                port_mappings=[
                    ecs.PortMapping(container_port=config["port"])
                ],
            )

            # X-Ray sidecar container
            task_def.add_container(
                f"XRay-{svc_name}",
                image=ecs.ContainerImage.from_registry(
                    "public.ecr.aws/xray/aws-xray-daemon:latest"
                ),
                logging=ecs.LogDrivers.aws_logs(
                    stream_prefix=f"{svc_name}-xray",
                    log_group=log_groups[svc_name],
                ),
                port_mappings=[
                    ecs.PortMapping(container_port=2000, protocol=ecs.Protocol.UDP)
                ],
                cpu=32,
                memory_limit_mib=64,
            )

            # Fargate Service
            fargate_service = ecs.FargateService(
                self,
                f"Service-{svc_name}",
                cluster=cluster,
                task_definition=task_def,
                desired_count=1,
                service_name=f"iot-fleet-{svc_name}",
                security_groups=[ecs_sg],
                assign_public_ip=False,
                vpc_subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS
                ),
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
                min_healthy_percent=0,
            )
            fargate_services[svc_name] = fargate_service

            # Target Group
            tg = elbv2.ApplicationTargetGroup(
                self,
                f"TG-{svc_name}",
                vpc=vpc,
                port=config["port"],
                protocol=elbv2.ApplicationProtocol.HTTP,
                target_type=elbv2.TargetType.IP,
                health_check=elbv2.HealthCheck(
                    path="/health",
                    interval=Duration.seconds(30),
                    timeout=Duration.seconds(5),
                    healthy_threshold_count=2,
                    unhealthy_threshold_count=3,
                ),
                target_group_name=f"iot-fleet-{svc_name}"[:32],
            )
            fargate_service.attach_to_application_target_group(tg)
            target_groups[svc_name] = tg

            # ALB Listener Rule (path-based routing)
            elbv2.ApplicationListenerRule(
                self,
                f"Rule-{svc_name}",
                listener=listener,
                priority=config["priority"],
                conditions=[
                    elbv2.ListenerCondition.path_patterns([config["path_pattern"]])
                ],
                target_groups=[tg],
            )

        # ---------------------------------------------------------------
        # CloudWatch Alarms
        # ---------------------------------------------------------------

        # P99 latency alarm per target group
        for svc_name, tg in target_groups.items():
            latency_alarm = cloudwatch.Alarm(
                self,
                f"P99Latency-{svc_name}",
                alarm_name=f"iot-fleet-{svc_name}-p99-latency",
                metric=cloudwatch.Metric(
                    namespace="AWS/ApplicationELB",
                    metric_name="TargetResponseTime",
                    dimensions_map={
                        "TargetGroup": tg.target_group_full_name,
                        "LoadBalancer": alb.load_balancer_full_name,
                    },
                    statistic="p99",
                    period=Duration.minutes(1),
                ),
                threshold=2.0,
                evaluation_periods=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                alarm_description=f"P99 latency exceeds 2s for {svc_name}",
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            latency_alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))

        # 5xx error rate alarm per service
        for svc_name, tg in target_groups.items():
            http_5xx_metric = cloudwatch.MathExpression(
                expression="(errors / requests) * 100",
                using_metrics={
                    "errors": cloudwatch.Metric(
                        namespace="AWS/ApplicationELB",
                        metric_name="HTTPCode_Target_5XX_Count",
                        dimensions_map={
                            "TargetGroup": tg.target_group_full_name,
                            "LoadBalancer": alb.load_balancer_full_name,
                        },
                        statistic="Sum",
                        period=Duration.minutes(1),
                    ),
                    "requests": cloudwatch.Metric(
                        namespace="AWS/ApplicationELB",
                        metric_name="RequestCount",
                        dimensions_map={
                            "TargetGroup": tg.target_group_full_name,
                            "LoadBalancer": alb.load_balancer_full_name,
                        },
                        statistic="Sum",
                        period=Duration.minutes(1),
                    ),
                },
                period=Duration.minutes(1),
            )

            error_alarm = cloudwatch.Alarm(
                self,
                f"5xxRate-{svc_name}",
                alarm_name=f"iot-fleet-{svc_name}-5xx-error-rate",
                metric=http_5xx_metric,
                threshold=5.0,
                evaluation_periods=3,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
                alarm_description=f"5xx error rate exceeds 5% for {svc_name}",
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )
            error_alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))

        # Alert storm detection (> 100 alerts/minute from alert-engine)
        alert_storm_alarm = cloudwatch.Alarm(
            self,
            "AlertStormDetection",
            alarm_name="iot-fleet-alert-storm-detection",
            metric=cloudwatch.Metric(
                namespace="IoTFleet/AlertEngine",
                metric_name="AlertsGenerated",
                dimensions_map={"Service": "alert-engine"},
                statistic="Sum",
                period=Duration.minutes(1),
            ),
            threshold=100,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description="Alert storm detected: more than 100 alerts/minute from alert-engine",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        alert_storm_alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))

        # Temperature threshold breach alarm
        temperature_alarm = cloudwatch.Alarm(
            self,
            "TemperatureThresholdBreach",
            alarm_name="iot-fleet-temperature-threshold-breach",
            metric=cloudwatch.Metric(
                namespace="IoTFleet/Telemetry",
                metric_name="DeviceTemperature",
                dimensions_map={"Service": "telemetry-ingest"},
                statistic="Maximum",
                period=Duration.minutes(1),
            ),
            threshold=85.0,
            evaluation_periods=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description="Device temperature exceeds 85C threshold",
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        temperature_alarm.add_alarm_action(cw_actions.SnsAction(alert_topic))

        # ---------------------------------------------------------------
        # CfnOutputs
        # ---------------------------------------------------------------
        CfnOutput(
            self,
            "AlbUrl",
            value=f"http://{alb.load_balancer_dns_name}",
            description="Application Load Balancer URL",
        )

        CfnOutput(
            self,
            "ClusterArn",
            value=cluster.cluster_arn,
            description="ECS Cluster ARN",
        )

        for svc_name, lg in log_groups.items():
            CfnOutput(
                self,
                f"LogGroup-{svc_name}-Output",
                value=lg.log_group_name,
                description=f"CloudWatch Log Group for {svc_name}",
            )

        CfnOutput(
            self,
            "RedisEndpoint",
            value=redis_host,
            description="ElastiCache Redis endpoint",
        )

        CfnOutput(
            self,
            "AlertTopicArn",
            value=alert_topic.topic_arn,
            description="SNS Topic ARN for fleet alerts",
        )

        # ---------------------------------------------------------------
        # Frontend — S3 + CloudFront (static React dashboard)
        # ---------------------------------------------------------------
        frontend_path = os.path.join(os.path.dirname(__file__), "..", "..", "frontend", "dist")

        frontend_bucket = s3.Bucket(
            self, "FrontendBucket",
            bucket_name=f"iot-fleet-dashboard-{self.account}-{self.region}",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        oai = cloudfront.OriginAccessIdentity(self, "FrontendOAI")
        frontend_bucket.grant_read(oai)

        distribution = cloudfront.Distribution(
            self, "FrontendDistribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3Origin(frontend_bucket, origin_access_identity=oai),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
            ),
            default_root_object="index.html",
            error_responses=[
                cloudfront.ErrorResponse(
                    http_status=404,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
                cloudfront.ErrorResponse(
                    http_status=403,
                    response_http_status=200,
                    response_page_path="/index.html",
                    ttl=Duration.seconds(0),
                ),
            ],
        )

        s3deploy.BucketDeployment(
            self, "FrontendDeployment",
            sources=[s3deploy.Source.asset(frontend_path)],
            destination_bucket=frontend_bucket,
            distribution=distribution,
            distribution_paths=["/*"],
        )

        CfnOutput(self, "DashboardUrl",
                  value=f"https://{distribution.distribution_domain_name}",
                  description="IoT Fleet Dashboard URL (CloudFront)")
        CfnOutput(self, "FrontendBucketName",
                  value=frontend_bucket.bucket_name,
                  description="S3 bucket hosting the dashboard")
