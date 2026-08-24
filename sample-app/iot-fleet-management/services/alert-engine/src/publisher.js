/**
 * Alert Publisher — sends triggered alerts to SNS and CloudWatch.
 */

const AWS = require('aws-sdk');
const logger = require('./logger');

const SNS_TOPIC_ARN = process.env.ALERT_SNS_TOPIC_ARN || '';
const REGION = process.env.AWS_REGION || 'us-east-1';

const sns = new AWS.SNS({ region: REGION });
const cloudwatch = new AWS.CloudWatch({ region: REGION });

async function publishAlert(alert) {
  // Publish to CloudWatch Metrics
  try {
    await cloudwatch.putMetricData({
      Namespace: 'IoTFleet/Alerts',
      MetricData: [{
        MetricName: 'AlertTriggered',
        Value: 1,
        Unit: 'Count',
        Dimensions: [
          { Name: 'DeviceId', Value: alert.device_id },
          { Name: 'MetricName', Value: alert.metric_name },
          { Name: 'Severity', Value: alert.severity },
        ],
      }],
    }).promise();
  } catch (err) {
    logger.error('Failed to publish CloudWatch metric', { error: err.message });
  }

  // Publish to SNS
  if (SNS_TOPIC_ARN) {
    try {
      await sns.publish({
        TopicArn: SNS_TOPIC_ARN,
        Subject: `[IoT Fleet Alert] ${alert.severity}: ${alert.metric_name} on ${alert.device_id}`,
        Message: JSON.stringify(alert, null, 2),
        MessageAttributes: {
          severity: { DataType: 'String', StringValue: alert.severity },
          device_id: { DataType: 'String', StringValue: alert.device_id },
        },
      }).promise();
    } catch (err) {
      logger.error('Failed to publish SNS alert', { error: err.message });
    }
  }
}

module.exports = { publishAlert };
