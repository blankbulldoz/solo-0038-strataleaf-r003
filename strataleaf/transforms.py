from .codec import dumps, field, loads
from .errors import Conflict, InvalidDocument, NotFound
from .mvcc import read_record

OPERATIONS = {"increment", "append_unique", "remove_member"}
MAX_ACTIONS = 1000


def json_equal(left, right):
    """Canonical JSON equality: booleans, null and numbers never compare equal."""
    return dumps(left) == dumps(right)


def validate_actions(actions):
    if not isinstance(actions, list) or not 1 <= len(actions) <= MAX_ACTIONS:
        raise InvalidDocument(f"transform requires between 1 and {MAX_ACTIONS} actions")
    normalized = []
    for action in actions:
        if not isinstance(action, dict):
            raise InvalidDocument("transform action must be an object")
        op = action.get("op")
        if op not in OPERATIONS:
            raise InvalidDocument("unknown transform operation: " + str(op))
        parameter = "amount" if op == "increment" else "value"
        if set(action) != {"op", "field", parameter}:
            raise InvalidDocument(f"{op} action requires exactly op, field and {parameter}")
        field({}, action["field"])
        payload = loads(dumps(action[parameter]))
        if op == "increment" and (isinstance(payload, bool) or not isinstance(payload, (int, float))):
            raise InvalidDocument("increment amount must be a number, not a boolean")
        normalized.append({"op": op, "field": action["field"], parameter: payload})
    return normalized


def _walk(document, parts):
    value = document
    for part in parts:
        if not isinstance(value, dict):
            raise InvalidDocument("transform field path does not resolve through an object")
        if part not in value:
            raise InvalidDocument("transform target field is missing")
        value = value[part]
    return value


def _store(document, parts, new_value):
    target = document
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = new_value


def apply_actions(document, actions):
    """Apply actions sequentially to a private copy; later actions see earlier results."""
    result = loads(dumps(document))
    for action in actions:
        parts = action["field"].split(".")
        current = _walk(result, parts)
        op = action["op"]
        if op == "increment":
            if isinstance(current, bool) or not isinstance(current, (int, float)):
                raise InvalidDocument("increment target must be an existing numeric field")
            _store(result, parts, current + action["amount"])
        elif op == "append_unique":
            if not isinstance(current, list):
                raise InvalidDocument("append_unique target must be an existing array field")
            value = action["value"]
            if not any(json_equal(item, value) for item in current):
                current.append(value)
        else:
            if not isinstance(current, list):
                raise InvalidDocument("remove_member target must be an existing array field")
            value = action["value"]
            _store(result, parts, [item for item in current if not json_equal(item, value)])
    return result


def transform_document(store, collection, key, actions, expected=None):
    from .transaction import WriteBatch

    current = read_record(store, collection, key)
    if current is None:
        raise NotFound("cannot transform a missing document")
    if expected is not None and expected != current.revision:
        raise Conflict("transform revision changed")
    return WriteBatch(store).transform(collection, key, actions, expected=current.revision,
                                       expires_at=current.expires_at).commit()
