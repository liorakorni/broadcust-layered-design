"""In-memory fakes for DynamoDB and Step Functions.

Only the behaviours the code actually depends on are modelled, but the ones that
are modelled are modelled faithfully - conditional writes really fail, and a
duplicate execution name really raises ExecutionAlreadyExists. A fake that always
succeeds would hide exactly the bugs these tests exist to catch.
"""

from __future__ import annotations

import re

from botocore.exceptions import ClientError


def _conditional_failure(op):
    return ClientError(
        {"Error": {"Code": "ConditionalCheckFailedException", "Message": "failed"}}, op
    )


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item["pk"]
        if ConditionExpression == "attribute_not_exists(pk)" and key in self.items:
            raise _conditional_failure("PutItem")
        self.items[key] = dict(Item)
        return {}

    def get_item(self, Key):
        item = self.items.get(Key["pk"])
        return {"Item": dict(item)} if item else {}

    def update_item(
        self,
        Key,
        UpdateExpression,
        ExpressionAttributeValues=None,
        ExpressionAttributeNames=None,
        ConditionExpression=None,
        ReturnValues=None,
    ):
        item = self.items.setdefault(Key["pk"], {"pk": Key["pk"]})
        names = ExpressionAttributeNames or {}
        values = ExpressionAttributeValues or {}

        def resolve(token):
            return names.get(token, token)

        if ConditionExpression:
            if ConditionExpression == "attribute_not_exists(executionArn)":
                if item.get("executionArn"):
                    raise _conditional_failure("UpdateItem")
            else:
                m = re.match(r"^(\w+) < :limit$", ConditionExpression)
                if m and item.get(m.group(1), 0) >= values[":limit"]:
                    raise _conditional_failure("UpdateItem")

        body = UpdateExpression[len("SET "):]
        updated = {}
        # Split on commas that separate assignments.
        for part in re.split(r",\s*(?=[#\w.]+\s*=)", body):
            target, expr = (s.strip() for s in part.split("=", 1))
            target = resolve(target)
            if "." in target:
                parent, child = target.split(".", 1)
                child = resolve(child)
                if not isinstance(item.get(parent), dict):
                    # DynamoDB will not create a parent map on the way to a
                    # nested attribute; the whole update is rejected instead.
                    # Creating it here would hide a write that fails in AWS.
                    raise ClientError(
                        {
                            "Error": {
                                "Code": "ValidationException",
                                "Message": (
                                    "The document path provided in the update "
                                    "expression is invalid for update"
                                ),
                            }
                        },
                        "UpdateItem",
                    )
                item[parent][child] = values[expr.strip()]
                continue
            inc = re.match(r"^([\w#.]+)\s*\+\s*(:\w+)$", expr)
            if inc:
                item[target] = item.get(target, 0) + values[inc.group(2)]
            else:
                item[target] = values[expr.strip()]
            updated[target] = item[target]
        return {"Attributes": updated} if ReturnValues else {}


class FakeStepFunctions:
    def __init__(self):
        self.started = []

    def start_execution(self, stateMachineArn, name, input):
        if any(e["name"] == name for e in self.started):
            raise ClientError(
                {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}},
                "StartExecution",
            )
        arn = f"{stateMachineArn.replace(':stateMachine:', ':execution:')}:{name}"
        self.started.append({"name": name, "arn": arn, "input": input})
        return {"executionArn": arn}


class FakeS3:
    """Minimal S3 with real IfNoneMatch semantics."""

    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, ContentType=None, IfNoneMatch=None):
        if IfNoneMatch == "*" and Key in self.objects:
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "exists"}},
                "PutObject",
            )
        self.objects[Key] = Body
        return {}

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject"
            )

        class Body:
            def __init__(self, data):
                self._data = data

            def read(self):
                return self._data

        return {"Body": Body(self.objects[Key])}
