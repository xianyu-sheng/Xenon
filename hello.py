"""
Module for calculating factorial of a number.
"""


def factorial(n: int) -> int:
    """
    Calculate the factorial of a non-negative integer.

    Args:
        n (int): A non-negative integer.

    Returns:
        int: The factorial of n.

    Raises:
        ValueError: If n is negative.
    """
    # Check for negative input
    if n < 0:
        raise ValueError("Factorial is not defined for negative numbers.")
    
    # Handle base case
    if n == 0 or n == 1:
        return 1
    
    # Iterative calculation to avoid recursion depth issues
    result = 1
    for i in range(2, n + 1):
        result *= i
    return result


# Test the function when the module is run directly
if __name__ == "__main__":
    # Example usage
    test_numbers = [0, 1, 5, 10]
    for num in test_numbers:
        print(f"Factorial of {num} is {factorial(num)}")