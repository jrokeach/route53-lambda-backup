"""AWS Route 53 Backup"""

import csv
import json
import os
import time
from datetime import datetime

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from hashlib import md5

# Set environmental variables

s3_bucket_name = os.environ.get("S3_BUCKET_NAME", None)
s3_bucket_region = os.environ.get("S3_BUCKET_REGION", None)
s3_bucket_folder = os.environ.get("S3_BUCKET_FOLDER", None)
s3_bucket_versioned = bool(int(os.environ.get("S3_BUCKET_VERSIONED"), 0) == 1)

if not s3_bucket_name or not s3_bucket_region:
    raise Exception(
        "S3_BUCKET_NAME and S3_BUCKET_REGION environment variables must be set."
    )


# Create client objects

s3 = boto3.client("s3", region_name="us-east-1")
route53 = boto3.client("route53", config=Config(retries={"max_attempts": 10}))


# Functions


def create_s3_bucket(bucket_name, bucket_region="us-east-1"):
    """Create an Amazon S3 bucket if it doesn't exist."""
    try:
        response = s3.head_bucket(Bucket=bucket_name)
        # Set versioning to a boolean; True if status returns "Enabled"
        versioning = (
            s3.get_bucket_versioning(Bucket=bucket_name).get("Status") == "Enabled"
        )
        if bool(versioning) != s3_bucket_versioned:
            raise (
                Exception(
                    "The bucket has versioning={} but this script is set to run as versioning={}".format(
                        versioning, s3_bucket_versioned
                    )
                )
            )
        return response
    except ClientError as e:
        if e.response["Error"]["Code"] != "404":
            print(e)
            return None
    # creating bucket in us-east-1 (N. Virginia) requires
    # no CreateBucketConfiguration parameter be passed
    if bucket_region == "us-east-1":
        response = s3.create_bucket(ACL="private", Bucket=bucket_name)
    else:
        response = s3.create_bucket(
            ACL="private",
            Bucket=bucket_name,
            CreateBucketConfiguration={"LocationConstraint": bucket_region},
        )
    # Check if the bucket is newly created (it should be), and if so, block public access policies
    # and apply appropriate versioning config
    if response.get("Location"):
        s3.put_public_access_block(
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
            Bucket=bucket_name,
        )
        if s3_bucket_versioned:
            s3.put_bucket_versioning(
                Bucket=bucket_name, VersioningConfiguration={"Status": "Enabled"}
            )
    return response


def upload_to_s3(filename, bucket_name, key, folder=None):
    """Upload a file to a folder in an Amazon S3 bucket."""
    # TODO: Change this logic so it doesn't require a folder.
    key = "/".join(filter(None, [folder, key]))
    # If the bucket is versioned, only upload if it doesn't match the existing version.
    if s3_bucket_versioned:
        # If the object doesn't already exist, no need to check ETag.
        try:
            response = s3.head_object(Bucket=bucket_name, Key=key)
        except ClientError as e:
            if e.response.get("Error", dict()).get("Code") == "404":
                # 404 error means the file doesn't exist. Proceed to upload.
                pass
            else:
                raise
        else:
            # We got a response, so we need to proceed to ETag comparison to see if the file has changed.
            with open(filename, "rb") as f:
                # TODO: Complete ETag computation for excessively large zones.
                #       Direct MD5 is only for single part uploads.
                if md5(f.read()).hexdigest().join(['"', '"']) == response["ETag"]:
                    return
    s3.upload_file(filename, bucket_name, key)


def get_route53_hosted_zones(next_zone=None):
    """Recursively returns a list of hosted zones in Amazon Route 53."""
    if next_zone:
        response = route53.list_hosted_zones_by_name(
            DNSName=next_zone[0], HostedZoneId=next_zone[1]
        )
    else:
        response = route53.list_hosted_zones_by_name()
    hosted_zones = response["HostedZones"]
    # if response is truncated, call function again with next zone name/id
    if response["IsTruncated"]:
        hosted_zones += get_route53_hosted_zones(
            (response["NextDNSName"], response["NextHostedZoneId"])
        )
    return hosted_zones


def get_route53_zone_records(zone_id, next_record=None):
    """Recursively returns a list of records of a hosted zone in Route 53."""
    if next_record:
        response = route53.list_resource_record_sets(
            HostedZoneId=zone_id,
            StartRecordName=next_record[0],
            StartRecordType=next_record[1],
        )
    else:
        response = route53.list_resource_record_sets(HostedZoneId=zone_id)
    zone_records = response["ResourceRecordSets"]
    # if response is truncated, call function again with next record name/id
    if response["IsTruncated"]:
        zone_records += get_route53_zone_records(
            zone_id, (response["NextRecordName"], response["NextRecordType"])
        )
    return zone_records


def get_record_value(record):
    """Return a list of values for a hosted zone record."""
    # test if record's value is Alias or dict of records
    try:
        value = [
            ":".join(
                [
                    "ALIAS",
                    record["AliasTarget"]["HostedZoneId"],
                    record["AliasTarget"]["DNSName"],
                ]
            )
        ]
    except KeyError:
        value = []
        for v in record["ResourceRecords"]:
            value.append(v["Value"])
    return value


def try_record(test, record):
    """Return a value for a record"""
    # test for Key and Type errors
    try:
        value = record[test]
    except KeyError:
        value = ""
    except TypeError:
        value = ""
    return value


def write_zone_to_csv(zone, zone_records):
    """Write hosted zone records to a csv file in /tmp/."""
    zone_file_name = "/tmp/" + zone["Name"] + "csv"
    # write to csv file with zone name
    with open(zone_file_name, "w", newline="") as csv_file:
        writer = csv.writer(csv_file)
        # write column headers
        writer.writerow(
            [
                "NAME",
                "TYPE",
                "VALUE",
                "TTL",
                "REGION",
                "WEIGHT",
                "SETID",
                "FAILOVER",
                "EVALUATE_HEALTH",
            ]
        )
        # loop through all the records for a given zone
        for record in zone_records:
            csv_row = [""] * 9
            csv_row[0] = record["Name"]
            csv_row[1] = record["Type"]
            csv_row[3] = try_record("TTL", record)
            csv_row[4] = try_record("Region", record)
            csv_row[5] = try_record("Weight", record)
            csv_row[6] = try_record("SetIdentifier", record)
            csv_row[7] = try_record("Failover", record)
            csv_row[8] = try_record(
                "EvaluateTargetHealth", try_record("AliasTarget", record)
            )
            value = get_record_value(record)
            # if multiple values (e.g., MX records), write each as its own row
            for v in value:
                csv_row[2] = v
                writer.writerow(csv_row)
    return zone_file_name


def write_zone_to_json(zone, zone_records):
    """Write hosted zone records to a json file in /tmp/."""
    zone_file_name = "/tmp/" + zone["Name"] + "json"
    # write to json file with zone name
    with open(zone_file_name, "w") as json_file:
        json.dump(zone_records, json_file, indent=4)
    return zone_file_name


## HANDLER FUNCTION ##


def lambda_handler(event, context):
    def gen_folder(zone=dict()):
        return "/".join(
            filter(None, [s3_bucket_folder, time_stamp, zone.get("Name", list())[:-1]])
        )

    """Handler function for AWS Lambda"""
    time_stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", datetime.utcnow().utctimetuple())
    if not create_s3_bucket(s3_bucket_name, s3_bucket_region):
        return False
    # bucket_response = create_s3_bucket(s3_bucket_name, s3_bucket_region)
    # if(not bucket_response):
    # return False
    hosted_zones = get_route53_hosted_zones()
    print(
        "Backing up {} hosted zones to {}/{}".format(
            len(hosted_zones), s3_bucket_name, gen_folder()
        )
    )
    for zone in hosted_zones:
        zone_folder = gen_folder(zone)
        zone_records = get_route53_zone_records(zone["Id"])
        upload_to_s3(
            write_zone_to_csv(zone, zone_records),
            s3_bucket_name,
            (zone["Name"] + "csv"),
            folder=zone_folder,
        )
        upload_to_s3(
            write_zone_to_json(zone, zone_records),
            s3_bucket_name,
            (zone["Name"] + "json"),
            folder=zone_folder,
        )
    return True


if __name__ == "__main__":
    lambda_handler(0, 0)
