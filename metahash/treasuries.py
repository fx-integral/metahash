from metahash.config import TESTING

# Production treasuries
PRODUCTION_TREASURIES: dict[str, str] = {
    "5Dnkprjf9fUrvWq3ZfFP8WrUNSjQws6UoHkbDfR1bQK8pFhW": "5GW6xj5wUpLBz7jCNp38FzkdS6DfeFdUTuvUjcn6uKH5krsn",
}

# Testing treasuries
TESTING_TREASURIES: dict[str, str] = {
    "5CtaFaSaJjKgbh1sqgR43knv2rq8RfzWHDPWHxNdiM1b1NRN": "5DLULtxCS9vA3pZRgSTd9gkvGrUwgNje2TNQeLibo9wUWjSS",
}

# Select treasury based on TESTING flag
VALIDATOR_TREASURIES: dict[str, str] = TESTING_TREASURIES if TESTING else PRODUCTION_TREASURIES
