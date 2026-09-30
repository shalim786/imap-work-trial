import * as pulumi from "@pulumi/pulumi";

// The assigned stack already owns a website. Preserve its resource identities.
const website =
  pulumi.getProject() === "imap-template" ? require("./website") : undefined;
export const url = website?.url;
export const bucketName = website?.bucketName;
export const distributionId = website?.distributionId;

// Unload only application resources for manual teardown; preserve the website.
const enabled =
  new pulumi.Config("agentmail-imap-aws").getBoolean("appEnabled") ?? true;
const trial = enabled ? require("./imap") : {};
export const imapHostname = trial.imapHostname;
export const autoScalingGroupName = trial.autoScalingGroupName;
export const launchTemplateId = trial.launchTemplateId;
export const artifactBucketName = trial.artifactBucketName;
export const applicationSha256 = trial.applicationSha256;
export const privateSubnetIds = trial.privateSubnetIds;
export const instanceSecurityGroupId = trial.instanceSecurityGroupId;
export const databaseEndpoint = trial.databaseEndpoint;
export const serviceEnabled = trial.serviceEnabled;
export const scaling = trial.scaling;
export const autoscalingPolicyArns = trial.autoscalingPolicyArns;
