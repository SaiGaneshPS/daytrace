// DT-33: Ask your day. A chat (ChatBox) answered by the local AI through the hub's tools (POST /ask): each answer
// shows the facts it used, the tools that found them and, when the hub sent a series, a small chart. When the AI is
// offline (or its model can't call tools) the page says why, stops offering questions, and checks again by itself.
import { useMemo } from "react";
import ChatBox, { AiOffline, ModelBadge, useAiStatus } from "../components/ChatBox";

export default function Ask() {
  const tz = useMemo(() => Intl.DateTimeFormat().resolvedOptions().timeZone, []);
  const ai = useAiStatus();
  const model = ai.data?.model ?? null;

  return (
    <div className="stack">
      <header className="page-head">
        <div>
          <p className="eyebrow">Answered from your data</p>
          <h1>Ask your day</h1>
        </div>
        {model && !ai.offline && <ModelBadge model={model} label="Runs on this device" />}
      </header>
      {ai.offline && (
        <AiOffline reason={ai.offline} onCheck={ai.reload}>
          Questions need the model. Everything else, like Today and the charts, still works.
        </AiOffline>
      )}
      {ai.noTools && (
        <AiOffline title="This model can't look things up" reason={ai.noTools} onCheck={ai.reload}>
          Stories still work with it.
        </AiOffline>
      )}
      <section className="card chat-card" aria-label="Ask">
        <ChatBox tz={tz} blocked={ai.offline ?? ai.noTools} />
      </section>
    </div>
  );
}
