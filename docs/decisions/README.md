# Engineering decisions

Decision records for API deprecation, realtime transport, uploads, worker scaling and Neo4j recovery. Each record explains its own rationale and scope. Check the implementation and any later decision before treating it as a current operational rule.

[Documentation home](../README.md) · [Current engineering contracts](../engineering/README.md)

## Documents

| File | Subject |
| --- | --- |
| [api-deprecation-window.md](api-deprecation-window.md) | Decision: deprecation window for orphaned C3/C4 routes |
| [frontend-realtime-transport.md](frontend-realtime-transport.md) | Frontend realtime transport rule |
| [neo4j-dr.md](neo4j-dr.md) | Decision: Neo4j knowledge-graph disaster recovery |
| [upload-path.md](upload-path.md) | Decision: canonical document-upload path |
| [worker-autoscaling.md](worker-autoscaling.md) | Decision: Celery worker queue-depth autoscaling (KEDA over prometheus-adapter) |
