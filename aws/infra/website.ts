import * as fs from "fs";
import * as path from "path";
import * as aws from "@pulumi/aws";
import * as pulumi from "@pulumi/pulumi";

const domain = new pulumi.Config().require("domain");
const zone = aws.route53.getZoneOutput({ name: domain, privateZone: false });

// Private bucket; only CloudFront can read it (via Origin Access Control below).
const bucket = new aws.s3.Bucket("site", { forceDestroy: true });

new aws.s3.BucketPublicAccessBlock("site", {
  bucket: bucket.id,
  blockPublicAcls: true,
  blockPublicPolicy: true,
  ignorePublicAcls: true,
  restrictPublicBuckets: true,
});

// Upload everything in www/ (flat).
const siteDir = path.join(__dirname, "../website-template/www");
const contentTypes: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
};
for (const file of fs.readdirSync(siteDir)) {
  new aws.s3.BucketObject(file, {
    bucket: bucket.id,
    key: file,
    source: new pulumi.asset.FileAsset(path.join(siteDir, file)),
    contentType: contentTypes[path.extname(file)] ?? "application/octet-stream",
  });
}

// CloudFront only accepts ACM certificates from us-east-1.
const usEast1 = new aws.Provider("us-east-1", { region: "us-east-1" });

const cert = new aws.acm.Certificate(
  "site",
  { domainName: domain, validationMethod: "DNS" },
  { provider: usEast1 },
);

const validationRecord = new aws.route53.Record("site-cert-validation", {
  zoneId: zone.zoneId,
  name: cert.domainValidationOptions[0].resourceRecordName,
  type: cert.domainValidationOptions[0].resourceRecordType,
  records: [cert.domainValidationOptions[0].resourceRecordValue],
  ttl: 300,
  allowOverwrite: true,
});

const certValidation = new aws.acm.CertificateValidation(
  "site",
  { certificateArn: cert.arn, validationRecordFqdns: [validationRecord.fqdn] },
  { provider: usEast1 },
);

const oac = new aws.cloudfront.OriginAccessControl("site", {
  originAccessControlOriginType: "s3",
  signingBehavior: "always",
  signingProtocol: "sigv4",
});

const distribution = new aws.cloudfront.Distribution("site", {
  enabled: true,
  isIpv6Enabled: true,
  aliases: [domain],
  defaultRootObject: "index.html",
  priceClass: "PriceClass_100",
  origins: [
    {
      originId: "s3",
      domainName: bucket.bucketRegionalDomainName,
      originAccessControlId: oac.id,
    },
  ],
  defaultCacheBehavior: {
    targetOriginId: "s3",
    viewerProtocolPolicy: "redirect-to-https",
    allowedMethods: ["GET", "HEAD"],
    cachedMethods: ["GET", "HEAD"],
    compress: true,
    cachePolicyId: "658327ea-f89d-4fab-a63d-7e88639e58f6", // AWS managed: CachingOptimized
  },
  viewerCertificate: {
    acmCertificateArn: certValidation.certificateArn,
    sslSupportMethod: "sni-only",
    minimumProtocolVersion: "TLSv1.2_2021",
  },
  restrictions: { geoRestriction: { restrictionType: "none" } },
});

new aws.s3.BucketPolicy("site", {
  bucket: bucket.id,
  policy: pulumi
    .all([bucket.arn, distribution.arn])
    .apply(([bucketArn, distributionArn]) =>
      JSON.stringify({
        Version: "2012-10-17",
        Statement: [
          {
            Effect: "Allow",
            Principal: { Service: "cloudfront.amazonaws.com" },
            Action: "s3:GetObject",
            Resource: `${bucketArn}/*`,
            Condition: { StringEquals: { "AWS:SourceArn": distributionArn } },
          },
        ],
      }),
    ),
});

for (const type of ["A", "AAAA"]) {
  new aws.route53.Record(`site-${type.toLowerCase()}`, {
    zoneId: zone.zoneId,
    name: domain,
    type,
    aliases: [
      {
        name: distribution.domainName,
        zoneId: distribution.hostedZoneId,
        evaluateTargetHealth: false,
      },
    ],
  });
}

export const url = `https://${domain}`;
export const bucketName = bucket.bucket;
export const distributionId = distribution.id;
