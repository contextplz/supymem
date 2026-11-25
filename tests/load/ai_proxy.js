import http from "k6/http";
import { check, sleep } from "k6";

// Local default: AI proxy health. Set AI_PROXY_URL for staging.
export const options = {
  vus: 2,
  duration: "15s",
  thresholds: {
    http_req_failed: ["rate<0.05"],
    http_req_duration: ["p(95)<1500"],
  },
};

export default function () {
  const base = __ENV.AI_PROXY_URL || "http://127.0.0.1:8088";
  const res = http.get(`${base}/health`);
  check(res, { "health 200": (r) => r.status === 200 });
  sleep(0.2);
}
