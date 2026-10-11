import http from "k6/http";
import { check, sleep } from "k6";

export const options = {
  scenarios: {
    reviewer_read_path: {
      executor: "constant-vus",
      vus: Number(__ENV.VUS || 10),
      duration: __ENV.DURATION || "30s",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<1000", "p(99)<1500"],
    checks: ["rate>0.99"],
  },
};

const baseUrl = __ENV.BASE_URL || "http://host.docker.internal:8000";
const reviewerKey = __ENV.REVIEWER_KEY;

export default function () {
  const ready = http.get(`${baseUrl}/health/ready`, { tags: { route: "ready" } });
  check(ready, { "readiness is 200": (response) => response.status === 200 });

  const queue = http.get(`${baseUrl}/api/v1/cases?limit=25`, {
    headers: { "X-Reviewer-Key": reviewerKey },
    tags: { route: "queue" },
  });
  check(queue, {
    "queue is 200": (response) => response.status === 200,
    "queue uses envelope": (response) => response.json("success") === true,
  });
  sleep(0.2);
}
