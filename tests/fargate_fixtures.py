"""SDK boundary fixtures only; no AWS client or network operation is used."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta


def profile_data():
    return {
        "region": "us-east-1",
        "cluster_arn": "arn:aws:ecs:us-east-1:000000000000:cluster/isolated-fixture",
        "task_definition_arn": "arn:aws:ecs:us-east-1:000000000000:task-definition/managed-fixture:1",
        "execution_role_arn": "arn:aws:iam::000000000000:role/isolated-build-execution",
        "image_uri": "000000000000.dkr.ecr.us-east-1.amazonaws.com/managed@sha256:"
        + "a" * 64,
        "subnet_ids": ["subnet-aaaa0001"],
        "security_group_ids": ["sg-bbbb0001"],
        "vpc_id": "vpc-cccc0001",
        "core_origin": "https://core.fixture.example",
        "container_name": "managed-builder",
    }


def task_definition(profile):
    return {
        "taskDefinitionArn": profile.task_definition_arn,
        "status": "ACTIVE",
        "executionRoleArn": profile.execution_role_arn,
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "2048",
        "memory": "4096",
        "runtimePlatform": {
            "operatingSystemFamily": "LINUX",
            "cpuArchitecture": "X86_64",
        },
        "volumes": [{"name": "workspace"}, {"name": "state"}, {"name": "tmp"}],
        "containerDefinitions": [
            {
                "name": "managed-builder",
                "image": profile.image_uri,
                "user": "10001:10001",
                "readonlyRootFilesystem": True,
                "linuxParameters": {
                    "capabilities": {"drop": ["ALL"]},
                    "initProcessEnabled": True,
                },
                "workingDirectory": "/opt/supymem",
                "entryPoint": ["python", "-m", "app.managed_api"],
                "mountPoints": [
                    {
                        "sourceVolume": "workspace",
                        "containerPath": "/workspace",
                        "readOnly": False,
                    },
                    {
                        "sourceVolume": "state",
                        "containerPath": "/state",
                        "readOnly": False,
                    },
                    {
                        "sourceVolume": "tmp",
                        "containerPath": "/tmp",
                        "readOnly": False,
                    },
                ],
            }
        ],
    }


class FakeAWS:
    def __init__(self, profile):
        self.profile = profile
        self.definition = task_definition(profile)
        self.requests, self.tasks, self.launches, self.stops = {}, {}, [], []
        self.lose_launch_response = False
        self.lose_stop_response = False
        self.rejected = False
        self.eni = {
            "NetworkInterfaceId": "eni-dddd0001",
            "VpcId": profile.vpc_id,
            "SubnetId": profile.subnet_ids[0],
            "Status": "in-use",
            "PrivateIpAddress": "10.91.0.11",
            "Groups": [{"GroupId": item} for item in profile.security_group_ids],
        }

    def describe_task_definition(self, **kwargs):
        return {"taskDefinition": deepcopy(self.definition)}

    def run_task(self, **request):
        self.launches.append(deepcopy(request))
        token = request["clientToken"]
        if token in self.requests:
            assert request == self.requests[token], (
                "same AWS token must replay the exact body"
            )
        if self.rejected:
            return {"tasks": [], "failures": [{"reason": "fixture-capacity"}]}
        if token not in self.requests:
            self.requests[token] = deepcopy(request)
            arn = (
                "arn:aws:ecs:us-east-1:000000000000:task/isolated-fixture/" + token[:32]
            )
            self.tasks[arn] = {
                "taskArn": arn,
                "clusterArn": self.profile.cluster_arn,
                "taskDefinitionArn": self.profile.task_definition_arn,
                "launchType": "FARGATE",
                "startedBy": request["startedBy"],
                "lastStatus": "RUNNING",
                "tags": deepcopy(request["tags"]),
                "pullStartedAt": datetime.now(UTC) - timedelta(seconds=90),
                "containers": [
                    {
                        "name": "managed-builder",
                        "imageDigest": self.profile.image_uri.split("@")[1],
                    }
                ],
                "attachments": [
                    {
                        "type": "ElasticNetworkInterface",
                        "status": "ATTACHED",
                        "details": [
                            {
                                "name": "networkInterfaceId",
                                "value": self.eni["NetworkInterfaceId"],
                            },
                            {
                                "name": "privateIPv4Address",
                                "value": self.eni["PrivateIpAddress"],
                            },
                        ],
                    }
                ],
            }
        if self.lose_launch_response:
            self.lose_launch_response = False
            raise TimeoutError("fixture: AWS accepted; response lost")
        return {
            "tasks": [
                deepcopy(row)
                for row in self.tasks.values()
                if row["startedBy"] == request["startedBy"]
            ]
        }

    def describe_tasks(self, *, tasks, **kwargs):
        return {
            "tasks": [deepcopy(self.tasks[arn]) for arn in tasks if arn in self.tasks]
        }

    def describe_network_interfaces(self, **kwargs):
        return {"NetworkInterfaces": [deepcopy(self.eni)]}

    def stop_task(self, *, task, **kwargs):
        self.stops.append(task)
        self.tasks[task].update(
            lastStatus="STOPPED", stoppedAt=datetime.now(UTC), stopCode="UserInitiated"
        )
        if self.lose_stop_response:
            self.lose_stop_response = False
            raise TimeoutError("fixture: AWS stopped; response lost")
        return {"task": deepcopy(self.tasks[task])}

    def list_tasks(self, *, startedBy, **kwargs):
        return {
            "taskArns": [
                arn for arn, row in self.tasks.items() if row["startedBy"] == startedBy
            ]
        }
