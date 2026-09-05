"""Local concurrent-load probe for Echo or a real local Ollama model."""
from __future__ import annotations
import argparse, concurrent.futures, io, json, statistics, time
from pathlib import Path
import auth, chat, config as cfg

def one(index: int, provider: str, model: str) -> float:
    configuration=cfg.Config(provider=provider, model=model, persona="default",
                             use_tools=False, max_tokens=256).resolve()
    session=chat.Session(configuration, chat.Style(False), auth.Principal(f"load-{index}", ("guest",), "load"), out=io.StringIO())
    started=time.perf_counter(); reply=session.ask(f"Reply with only: health check {index} acknowledged")
    if not reply: raise RuntimeError("empty response")
    return time.perf_counter()-started

def run(requests: int=100, concurrency: int=20, provider: str="echo", model: str="") -> dict:
    started=time.perf_counter()
    latencies=[];errors=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures=[pool.submit(one,index,provider,model) for index in range(requests)]
        for future in concurrent.futures.as_completed(futures):
            try:latencies.append(future.result())
            except Exception as exc:errors.append(type(exc).__name__+": "+str(exc)[:160])
    ordered=sorted(latencies)
    return {"provider":provider,"model":model or "provider-default","requests":requests,"concurrency":concurrency,"completed":len(latencies),"failures":len(errors),
            "elapsed_seconds":round(time.perf_counter()-started,3),
            "average_ms":round(statistics.mean(latencies)*1000,2) if latencies else None,
            "median_ms":round(statistics.median(latencies)*1000,2) if latencies else None,
            "p95_ms":round(ordered[max(0,int(len(ordered)*.95)-1)]*1000,2) if ordered else None,
            "max_ms":round(max(latencies)*1000,2) if latencies else None,
            "errors":errors[:10]}

def main()->int:
    p=argparse.ArgumentParser();p.add_argument("--requests",type=int,default=100);p.add_argument("--concurrency",type=int,default=20);p.add_argument("--provider",choices=["echo","ollama"],default="echo");p.add_argument("--model",default="");p.add_argument("--output",type=Path);a=p.parse_args()
    report=run(a.requests,a.concurrency,a.provider,a.model)
    if a.output:a.output.parent.mkdir(parents=True,exist_ok=True);a.output.write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(report,indent=2));return 0 if report["failures"]==0 else 1
if __name__=="__main__":raise SystemExit(main())
