# API documentation

This documentation describes the HTTP endpoints currently available in the FastAPI application.

## Available API areas

- Health check
- [Webhook endpoint configuration](webhook-endpoints.md)

## Health check

```text
GET /health
```

The health check returns HTTP 200 and can be used to confirm that the application is available.

```json
{
  "status": "ok"
}
```

## Webhook endpoint configuration

The API supports creating webhook endpoint configurations and listing stored configurations.

Available routes:

- `POST /webhook-endpoints`
- `GET /webhook-endpoints`

See [Webhook endpoint API](webhook-endpoints.md) for request, response, and validation details.

## Interactive documentation

FastAPI exposes interactive API documentation when the application is running locally:

- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`

## Navigation

- [Webhook endpoint API](webhook-endpoints.md)
- [Documentation index](../index.md)
- [Development setup](../development.md)
- [Project README](../../README.md)
