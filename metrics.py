from prometheus_client import start_http_server, Summary, Counter

REQUEST_TIME = Summary('request_processing_seconds', 'Time spent processing request')
REQUEST_COUNT = Counter('request_count', 'Total Requests Processed')
