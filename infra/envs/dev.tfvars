# The dev environment. Nothing in this file is a secret, which is why it is committed.
# Machine specific values, such as allowed_cidrs, go in a gitignored dev.local.tfvars.

environment    = "dev"
aws_account_id = "277707137200"

# One AZ's worth of interface endpoints. Each costs about 9.50 USD a month per AZ.
interface_endpoint_az_count = 1

# A throwaway environment: destroy must work, and recreate must work after it.
destroyable = true

# The account's Bedrock token quota is 0 (AWS Support case 178990624000702), so dev runs on
# the documented fallback. Going back to the target design is these three lines:
#   llm_provider = "bedrock"
#   llm_model_id = "apac.amazon.nova-pro-v1:0"
#   enable_nat   = false
llm_provider = "openai"
llm_model_id = "gpt-4o-mini"
enable_nat   = true
