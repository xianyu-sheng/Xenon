def parse_left(value):
    value = value.strip()
    return value.split(":", 1)[0]


def parse_right(value):
    value = value.strip()
    return value.split(":", 1)[-1]
