#!/usr/bin/env python3

import os
import time
import click
import boto3
import subprocess
import json

# Configure the AWS region and the EC2 client
region = "us-east-1"
ec2_client = boto3.client("ec2", region_name=region)

instance_templates = {
    "nano": {
        "template_id": "lt-0e0b43a5b92c4d420",  # test-template-nano
        "template_version": "1",
    },
    "gpu": {
        "template_id": "lt-01849062ec450a84b",  # efficient-dl
        "template_version": "3",
    },
}

volume_id = "vol-03e082be1ec238e52"
key_path = "~/Dropbox/virginia.cer"


@click.group()
def cli():
    pass


def run_instance(instance_type):
    # Launch an instance from the launch template
    response = ec2_client.run_instances(
        LaunchTemplate={
            "LaunchTemplateId": instance_templates[instance_type]["template_id"],
            "Version": instance_templates[instance_type]["template_version"],
        },
        MaxCount=1,
        MinCount=1,
        Placement={"AvailabilityZone": "us-east-1a"},
    )

    # Retrieve the instance ID and public IP address
    instance_id = response["Instances"][0]["InstanceId"]
    return instance_id


def get_instance_ip(instance_id):
    response = ec2_client.describe_instances(InstanceIds=[instance_id])
    instance_ip = response["Reservations"][0]["Instances"][0]["PublicIpAddress"]
    return instance_ip


@cli.command()
@click.argument("instance_type", type=click.Choice(instance_templates.keys()))
@click.option("--volume_device", default="/dev/nvme2n1")
def up(instance_type, volume_device):
    # If the instance info file exists, load the instance ID and IP address
    instance_id, instance_ip = load_instance_info()
    if instance_id is not None:
        click.echo(f"Instance {instance_id} already provisioned with IP {instance_ip}.")
        return

    click.echo("Provisioning instance...")
    instance_id = run_instance(instance_type)

    # Wait for the instance to start running
    waiter = ec2_client.get_waiter("instance_running")
    click.echo("Waiting for instance to start running...")
    waiter.wait(InstanceIds=[instance_id])

    instance_ip = get_instance_ip(instance_id)
    click.echo(f"Instance {instance_id} provisioned with IP {instance_ip}.")

    # Save the instance id and IP address to a file
    save_instance_info(instance_id, instance_ip)

    click.echo("Waiting for SSH to be available...")
    time.sleep(10)

    attach_volume(instance_id, instance_ip, volume_device)


def attach_volume(instance_id, instance_ip, volume_device):
    # Attach the volume
    ec2_client.attach_volume(
        Device="/dev/sdf",
        InstanceId=instance_id,
        VolumeId=volume_id,
    )

    # Wait for the volume to be attached
    waiter = ec2_client.get_waiter("volume_in_use")
    click.echo("Waiting for volume to be attached...")
    waiter.wait(VolumeIds=[volume_id])

    # Mount volume
    click.echo("Mounting volume...")
    execute_ssh(instance_ip, ["sudo", "mkdir", "/mnt/external"])
    execute_ssh(instance_ip, ["sudo", "chown", "ubuntu", "/mnt/external"])
    execute_ssh(instance_ip, ["sudo", "mount", volume_device, "/mnt/external"])


def execute_ssh(instance_ip, command, check=True):
    command = [
        "ssh",
        "-i",
        key_path,
        "-o",
        "StrictHostKeyChecking=no",
        "ubuntu@" + instance_ip,
        *command,
    ]

    click.echo(f"Executing command: {' '.join(command)}")

    if check:
        subprocess.check_call(command)
    else:
        subprocess.call(command)


def save_instance_info(instance_id, instance_ip):
    with open("instance_info.json", "w") as f:
        json.dump({"instance_id": instance_id, "instance_ip": instance_ip}, f)


def load_instance_info():
    try:
        with open("instance_info.json", "r") as f:
            instance_info = json.load(f)
        return instance_info["instance_id"], instance_info["instance_ip"]
    except FileNotFoundError:
        return None, None


@cli.command()
def terminate():
    # Load the instance ID from the file
    instance_id = load_instance_info()[0]
    if instance_id is None:
        click.echo("No instance provisioned.")
        return

    click.echo(f"Terminating instance {instance_id}.")

    # Terminate the instance
    ec2_client.terminate_instances(InstanceIds=[instance_id])

    # Wait for the instance to be terminated
    waiter = ec2_client.get_waiter("instance_terminated")
    waiter.wait(InstanceIds=[instance_id])

    # Delete the instance info file
    subprocess.call(["rm", "instance_info.json"])

    click.echo(f"Instance {instance_id} terminated.")


@cli.command()
def ssh():
    # Load the instance IP address from the file
    instance_ip = load_instance_info()[1]
    if instance_ip is None:
        click.echo("No instance provisioned.")
        return

    click.echo(f"SSHing into instance with IP {instance_ip}.")

    # SSH into the instance
    execute_ssh(instance_ip, ["-L", "8888:localhost:8888"], check=False)


@cli.command()
@click.argument("from_path")
@click.argument("to_path")
def scp(from_path, to_path):
    # Load the instance IP address from the file
    instance_ip = load_instance_info()[1]
    if instance_ip is None:
        click.echo("No instance provisioned.")
        return

    click.echo(f"Copying {from_path} to {to_path} on instance with IP {instance_ip}.")

    def normalize_path(path):
        if path.startswith("ec2:"):
            return f"ubuntu@{instance_ip}:{path[4:]}"
        return os.path.abspath(path)

    from_path = normalize_path(from_path)
    to_path = normalize_path(to_path)

    # Copy file to instance
    subprocess.check_call(
        [
            "scp",
            "-i",
            key_path,
            "-o",
            "StrictHostKeyChecking=no",
            from_path,
            to_path,
        ]
    )


if __name__ == "__main__":
    cli()
