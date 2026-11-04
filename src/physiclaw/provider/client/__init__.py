"""Shared API client infrastructure for chat and decision adapters.

Credential and endpoint resolution, owned client lifetime, and HTTP failure
metadata live here. This package knows no service registrations, concrete
vendor adapters, conversation formats, or execution policy.
"""
