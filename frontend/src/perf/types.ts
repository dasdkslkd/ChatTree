export interface PerfConfig {
  enabled: boolean;
  perf_run_id: string;
  sample_rate: number;
  max_attr_length: number;
  max_batch_events: number;
}

export interface FrontendPerfEvent {
  type: 'mark' | 'span';
  name: string;
  duration_ms?: number;
  run_id?: string | null;
  conversation_id?: string | null;
  node_id?: string | null;
  client_run_id?: string | null;
  attrs?: Record<string, unknown>;
}
