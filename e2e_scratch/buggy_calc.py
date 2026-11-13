"""Small statistics helpers used by the acceptance test."""


def average(values):
    total = 0
    count = 0
    for value in values:
        total += value
        count += 1
    if count == 0:
        return 0
    return total / count


def spread(values):
    return max(values) - min(values)
