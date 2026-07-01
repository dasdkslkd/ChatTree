import { useEffect, useMemo, useState } from 'react';
import { Loader2, Play, Square } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { capabilitiesApi, type CapabilityAgent } from '../../api/capabilities';
import { agentsApi } from '../../api/agents';
import { workflowsApi } from '../../api/workflows';
import { streamManager, type StreamState } from '../../services/streamManager';

interface Props {
  conversationId: string | null;
  runs: StreamState[];
}

export function RunStatusPanel({ conversationId, runs }: Props) {
  const [agents, setAgents] = useState<CapabilityAgent[]>([]);
  const [agentName, setAgentName] = useState('');
  const [agentInput, setAgentInput] = useState('');
  const [workflowScript, setWorkflowScript] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    capabilitiesApi.list()
      .then((inventory) => {
        if (cancelled) return;
        setAgents(inventory.agents || []);
        if (!agentName && inventory.agents?.[0]?.name) setAgentName(inventory.agents[0].name);
      })
      .catch(() => {
        if (!cancelled) setAgents([]);
      });
    return () => { cancelled = true; };
  }, []);

  const activeRuns = useMemo(
    () => runs.filter((run) => run.status === 'streaming' || run.status === 'stopped'),
    [runs],
  );

  const startSubagent = async () => {
    if (!conversationId || !agentName || !agentInput.trim()) return;
    setBusy(true);
    try {
      const run = await agentsApi.startRun(conversationId, agentName, { input: agentInput });
      void streamManager.resumeStream(conversationId, '', run.run_id, 0);
      setAgentInput('');
    } finally {
      setBusy(false);
    }
  };

  const startWorkflow = async () => {
    if (!conversationId || !workflowScript.trim()) return;
    setBusy(true);
    try {
      const run = await workflowsApi.startRun(conversationId, {
        script: workflowScript,
        args: {},
      });
      void streamManager.resumeStream(conversationId, '', run.run_id, 0);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mx-2 mt-3 flex flex-col gap-3 border-t pt-3" style={{ borderColor: 'var(--border)' }}>
      <div className="px-1 text-xs font-semibold" style={{ color: 'var(--fg-tertiary)' }}>运行</div>
      <div className="flex flex-col gap-1.5">
        {activeRuns.length === 0 && (
          <div className="px-1 text-xs" style={{ color: 'var(--fg-tertiary)' }}>无运行任务</div>
        )}
        {activeRuns.map((run) => (
          <div
            key={run.runId}
            className="flex items-center gap-2 rounded-md px-2 py-1.5 text-xs"
            style={{ background: 'var(--bg-button-tertiary-hover)', color: 'var(--fg-secondary)' }}
          >
            <Loader2 className="h-3.5 w-3.5 shrink-0" />
            <span className="min-w-0 flex-1 truncate">{run.kind} · {run.runId.slice(0, 12)}</span>
            <button
              type="button"
              className="border-0 bg-transparent p-0"
              onClick={() => streamManager.stopRun(run.runId)}
              title="停止"
            >
              <Square className="h-3.5 w-3.5" />
            </button>
          </div>
        ))}
      </div>

      <div className="flex flex-col gap-1.5">
        <div className="px-1 text-xs font-semibold" style={{ color: 'var(--fg-tertiary)' }}>Subagent</div>
        <select
          className="h-8 rounded-md border bg-transparent px-2 text-xs"
          style={{ borderColor: 'var(--border)', color: 'var(--fg-secondary)' }}
          value={agentName}
          onChange={(event) => setAgentName(event.target.value)}
        >
          {agents.map((agent) => (
            <option key={agent.name} value={agent.name}>{agent.name}</option>
          ))}
        </select>
        <Input
          value={agentInput}
          onChange={(event) => setAgentInput(event.target.value)}
          placeholder="任务"
          className="h-8 text-xs"
        />
        <Button size="sm" variant="outline" onClick={startSubagent} disabled={!conversationId || !agentName || !agentInput.trim() || busy}>
          <Play className="mr-1 h-3.5 w-3.5" />
          启动
        </Button>
      </div>

      <div className="flex flex-col gap-1.5">
        <div className="px-1 text-xs font-semibold" style={{ color: 'var(--fg-tertiary)' }}>Workflow</div>
        <textarea
          value={workflowScript}
          onChange={(event) => setWorkflowScript(event.target.value)}
          placeholder={'return await phase("检查", async () => await agent(args.task || "任务", { agentType: "workflow-worker" }))'}
          className="min-h-20 resize-y rounded-md border bg-transparent px-2 py-1.5 text-xs outline-none"
          style={{ borderColor: 'var(--border)', color: 'var(--fg-secondary)' }}
        />
        <Button size="sm" variant="outline" onClick={startWorkflow} disabled={!conversationId || !workflowScript.trim() || busy}>
          <Play className="mr-1 h-3.5 w-3.5" />
          运行
        </Button>
      </div>
    </div>
  );
}
