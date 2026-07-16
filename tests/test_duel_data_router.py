from __future__ import annotations

import unittest

from fastapi import HTTPException
from starlette.requests import Request

from module.duel_data.models import DuelRecommendation
from module.server.duel_data_router import (
    _require_local_duel_access,
    duel_data_app,
)


class DuelDataRouterContractTest(unittest.TestCase):
    def test_public_recommendation_matches_sse_wire_shape(self):
        recommendation = DuelRecommendation.model_validate(
            {
                "state": "SELF_PICK",
                "phase": "SELF_PICK",
                "mode": "recommend",
                "shishen_id": 101,
                "shikigami_id": 101,
                "source": "rule",
                "score": 0.95,
                "confidence": 0.99,
                "recognition_confidence": 0.99,
                "recommendation_confidence": 0.99,
                "sample_size": 20,
                "explanation": "fixture",
                "reason": "fixture",
                "recommendations": [
                    {
                        "shishen_id": 101,
                        "shikigami_id": 101,
                        "source": "rule",
                        "score": 0.95,
                        "confidence": 0.99,
                        "sample_size": 20,
                        "reason": "fixture",
                        "evidence_sources": ["rule"],
                    }
                ],
                "evidence_sources": ["rule"],
            }
        )
        self.assertEqual(101, recommendation.recommendations[0].shishen_id)

    def test_expected_routes_and_methods_are_registered(self):
        routes = {
            (route.path, method)
            for route in duel_data_app.routes
            for method in (route.methods or set())
        }
        expected = {
            ("/duel-data/matches", "GET"),
            ("/duel-data/matches/{match_id}", "GET"),
            ("/duel-data/matches/{match_id}", "PATCH"),
            ("/duel-data/summary", "GET"),
            ("/duel-data/live", "GET"),
        }
        self.assertTrue(expected.issubset(routes), expected - routes)
        self.assertFalse(
            any(path.startswith("/duel-data/import/") for path, _ in routes),
            routes,
        )

    @staticmethod
    def _request(client_host: str, origin: str | None = None) -> Request:
        headers = []
        if origin is not None:
            headers.append((b"origin", origin.encode("ascii")))
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/duel-data/summary",
                "headers": headers,
                "client": (client_host, 12345),
                "server": ("127.0.0.1", 22270),
                "scheme": "http",
                "query_string": b"",
            }
        )

    def test_private_duel_routes_allow_only_local_clients_and_origins(self):
        _require_local_duel_access(self._request("127.0.0.1"))
        _require_local_duel_access(
            self._request("::1", "http://localhost:4321")
        )
        for request in (
            self._request("192.168.1.50"),
            self._request("127.0.0.1", "https://malicious.example"),
        ):
            with self.subTest(client=request.client, origin=request.headers.get("origin")):
                with self.assertRaises(HTTPException) as caught:
                    _require_local_duel_access(request)
                self.assertEqual(403, caught.exception.status_code)


if __name__ == "__main__":
    unittest.main()
