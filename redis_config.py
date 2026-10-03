import os

REDIS_HOST = os.getenv('SHARED_REDIS_HOST', 'localhost')
REDIS_PORT = os.getenv('SHARED_REDIS_PORT', 6379)
