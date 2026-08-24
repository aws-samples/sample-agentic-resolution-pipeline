#!/usr/bin/env python3
import os
import aws_cdk as cdk

from stacks.iot_fleet_stack import IoTFleetStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

IoTFleetStack(app, "IoTFleetStack", env=env)

app.synth()
