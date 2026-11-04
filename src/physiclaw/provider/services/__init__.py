"""Vendor registration and construction of chat or decision services.

This is the composition layer: it may know concrete adapters. Adapters and
shared client/catalog code must not depend on it. Import registry or factory
explicitly; application callers use the public physiclaw.provider facade.
"""
