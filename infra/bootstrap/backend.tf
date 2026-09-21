# The bootstrap stack keeps its state in the bucket it creates.
#
# That is circular exactly once, on the first run in a new account, when the bucket does not
# exist yet. The first run therefore uses local state, and the state is then moved in:
#
#   mv backend.tf backend.tf.off                  # the bucket does not exist yet
#   terraform init && terraform apply             # local state
#   mv backend.tf.off backend.tf
#   terraform init -migrate-state -backend-config=backend.hcl
#
# Tearing the account down is the same thing backwards, because the bucket is one of the
# resources being destroyed and cannot hold the state of its own destruction:
#
#   mv backend.tf backend.tf.off
#   terraform init -migrate-state                 # back to a local file
#   terraform destroy
#
# Why it is not simply left local: a local state file lives in one directory on one machine
# and is gitignored. Losing that directory loses the ability to manage these resources,
# including the CI roles, without importing all of them again by hand.
terraform {
  backend "s3" {}
}
