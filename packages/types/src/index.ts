export type ComponentHealth = {
  status: "up" | "down";
  latencyMs?: number;
  detail?: string;
};

export type HealthResponse = {
  status: "ok" | "degraded";
  service: string;
  environment: string;
  version: string;
  checks?: Record<string, ComponentHealth>;
};
