import os

# 32+ chars, not a placeholder -> resolve_secret() accepts it and writes no key file.
os.environ.setdefault("GLASSOPS_SECRET_KEY", "test-secret-key-for-pytest-0123456789abcdef")
