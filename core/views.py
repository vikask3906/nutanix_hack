from django.db import connection
from django.http import JsonResponse


def healthz(_request):
    """Liveness + database reachability.

    Deliberately checks the DB: in Cascade the database *is* the queue, the timer
    and the state store, so an API that cannot reach Postgres is not healthy in any
    useful sense.
    """
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
    except Exception as exc:  # noqa: BLE001 - report any failure to the caller
        return JsonResponse({"status": "unhealthy", "database": str(exc)}, status=503)

    return JsonResponse({"status": "ok", "database": "ok"})
