/**
 * Core Web Vitals via Google's `web-vitals` library — the same primitive
 * the OpenTelemetry browser work builds on. The raw attribution object is
 * deliberately never read (it can carry element selectors and URLs); only
 * the five bounded fields below leave this module.
 */

import { onCLS, onFCP, onINP, onLCP, onTTFB, type Metric } from "web-vitals";

export interface VitalInput {
  name: string;
  value: number;
  rating: string;
  id: string;
  navigationType?: string;
}

export function registerVitals(report: (metric: VitalInput) => void): void {
  const handler = (metric: Metric) => {
    report({
      name: metric.name,
      value: metric.value,
      rating: metric.rating,
      id: metric.id,
      navigationType: metric.navigationType,
    });
  };
  onLCP(handler);
  onCLS(handler);
  onINP(handler);
  onTTFB(handler);
  onFCP(handler);
}
