import boto3
client = boto3.client('bedrock', region_name='us-east-1')
response = client.list_foundation_models(byProvider='anthropic')
print(f"{'Model ID':<60} | {'Status'}")
print("-" * 80)
for model in response['modelSummaries']:
    print(f"{model['modelId']:<60} | {model['modelLifecycle']['status']}")
