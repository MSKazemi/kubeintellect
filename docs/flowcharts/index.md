# Architecture Flowcharts

Visual reference for KubeIntellect's internal topology, agent routing logic, and synthesis pipelines.
All diagrams are rendered from source — they always reflect the current implementation.

---

## Diagrams

=== ":material-sitemap: System Architecture"

    Full system view: every layer from user query to Kubernetes API, including the persistence
    stack, observability infrastructure, and security controls.

    ```mermaid
    ---
    config:
      flowchart:
        curve: basis
        rankSpacing: 55
        nodeSpacing: 28
    ---
    graph LR

    USER(["👤 User"]):::ext
    K8S(["⎈ Kubernetes\nCluster(s)"]):::k8s

    subgraph CORE["Core System"]
      direction TB

      UIL["🖥️ User Interaction Layer\nLibreChat · REST /chat/completions"]:::layer
      QPM["🔍 Query Processing Module\nLLM scope filter · OOS rejection\nambiguity clarification"]:::layer

      subgraph ORCH["Task Orchestration Layer"]
        direction TB
        MO["🧠 Memory Orchestrator\n4-tier parallel assembly ≤550 tokens\nReflections · Failure Hints · Prefs · Dyn.Tools"]:::mem
        SUP{{"🎛️ Supervisor LLM\nLangGraph StateGraph\nStateGraph routing"}}:::sup
        HITL["🔒 HITL Gates\ninterrupt_before:\nCodeGenerator · Apply"]:::hitl
      end

      subgraph AGENTS["Agent & Tool Execution Layer  (ReAct loops)"]
        direction LR
        subgraph RAGENTS["Read / Inspect Agents"]
          direction TB
          A_LOG["Logs"]:::agent
          A_CMS["ConfigMapsSecrets"]:::agent
          A_RBC["RBAC"]:::agent
          A_MET["Metrics"]:::agent
          A_SEC["Security"]:::agent
        end
        subgraph WAGENTS["Write / Exec Agents"]
          direction TB
          A_LC["Lifecycle"]:::agent
          A_EX["Execution"]:::agent
          A_DEL["Deletion\n(conv. confirm)"]:::agent
          A_INF["Infrastructure"]:::agent
          A_AP["Apply"]:::agent
        end
        subgraph SYNTH["Synthesis Agents"]
          direction TB
          A_DTE["DynamicToolsExecutor"]:::dyn
          A_CG["CodeGenerator\n(generate→test→register)"]:::codegen
        end
        subgraph DIAORCH["DiagnosticsOrchestrator  (fan-out)"]
          direction LR
          DO["Dispatch"]:::diag
          DL["Logs\nsignal"]:::diag
          DM["Metrics\nsignal"]:::diag
          DE["Events\nsignal"]:::diag
          DC["Collect\n(barrier)"]:::diag
          DO -->|"Send"| DL
          DO -->|"Send"| DM
          DO -->|"Send"| DE
          DL --> DC
          DM --> DC
          DE --> DC
        end
      end

      KIL["⎈ Kubernetes Interaction Layer\nK8s Python Client · SSH tunnel"]:::layer
    end

    subgraph SUPP["Supporting Infrastructure"]
      direction TB

      LLMGW["🔁 LLM Gateway\n7 providers: Azure · OpenAI · Anthropic\nGoogle · Bedrock · Ollama · LiteLLM"]:::sup

      subgraph PERSIST["Persistence Layer  (PostgreSQL)"]
        direction TB
        PG_CHK["LangGraph Checkpoints\n(HITL resume · workflow state)"]:::pgbox
        PG_CTX["Conversation Context\n(sticky ns · resource · tool)"]:::pgbox
        PG_FP["Failure Patterns  ×30\n(keyword match → hint injection)"]:::pgbox
        PG_PREF["User Preferences\n(verbosity · format · ns · cluster)"]:::pgbox
        PG_TR["Tool Registry\n+ PVC /mnt/runtime-tools"]:::pgbox
        PG_AUD["Audit Log\n(user · query · agents · latency)"]:::pgbox
      end

      subgraph OBS["Observability Stack"]
        direction TB
        OB_LF["🔭 Langfuse\nSelf-hosted · per-conv. traces\ntoken · cost · latency"]:::obs
        OB_PR["📊 Prometheus + Grafana\n11 custom metrics\nagent_invocations · tool_calls · HITL"]:::obs
        OB_LK["📜 Loki + Promtail\nStructured JSON logs\nkube-event-exporter"]:::obs
      end

      subgraph SEC_GOV["Security & Governance"]
        direction TB
        SG_RB["K8s RBAC (Helm)\nget/list/watch default\nwrite ops feature-flagged"]:::sec
        SG_SB["CodeGenerator Sandbox\nAST filter · exec timeout\nSHA-256 hash"]:::sec
      end

      TOS["✂️ Tool Output Summarizer\nToken-budget truncation\nlogs · YAML · list-type (top-k)"]:::mem
    end

    USER -->|"NL query"| UIL
    UIL -->|"response"| USER
    UIL --> QPM
    QPM --> MO
    MO --> SUP
    SUP -->|"route"| RAGENTS
    SUP -->|"route"| WAGENTS
    SUP -->|"HITL"| HITL
    HITL -->|"approve"| SYNTH
    SUP -->|"diagnose"| DO
    DC -->|"aggregated evidence"| SUP
    RAGENTS --> SUP
    WAGENTS --> SUP
    SYNTH --> SUP
    SUP -->|"FINISH"| UIL
    KIL -->|"API calls"| K8S
    AGENTS --> KIL
    K8S -->|"cluster data"| KIL
    SUP -.->|"LLM calls"| LLMGW
    LLMGW -.-> OB_LF
    SUP -.-> PERSIST
    HITL -.-> PG_CHK
    A_CG -.-> PG_TR
    SUP -.-> OB_PR

    classDef ext fill:#dfe6e9,stroke:#636e72,color:#2d3436,font-weight:bold
    classDef k8s fill:#d5f5e3,stroke:#27ae60,color:#1a5e32,font-weight:bold
    classDef layer fill:#ebf5fb,stroke:#2e86c1,color:#1a5276,font-weight:bold
    classDef sup fill:#e8daef,stroke:#8e44ad,color:#4a235a,font-weight:bold
    classDef mem fill:#e8d5f5,stroke:#7d3c98,color:#4a235a
    classDef hitl fill:#fde8d8,stroke:#d35400,color:#6e2c00,font-weight:bold
    classDef agent fill:#d6eaf8,stroke:#2e86c1,color:#1a5276
    classDef dyn fill:#d5f5e3,stroke:#1e8449,color:#1a5e32
    classDef codegen fill:#fae5d3,stroke:#ca6f1e,color:#6e2c00,font-weight:bold
    classDef diag fill:#d1f2eb,stroke:#148f77,color:#0e6655
    classDef obs fill:#fef9e7,stroke:#d4ac0d,color:#7d6608
    classDef pgbox fill:#f4ecf7,stroke:#7d3c98,color:#4a235a
    classDef sec fill:#fdedec,stroke:#c0392b,color:#78281f
    ```

=== ":material-transit-connection-variant: Supervisor Routing Flow"

    How the Supervisor LLM decides which agent handles each request — including HITL gates,
    memory assembly, DiagnosticsOrchestrator fan-out, and CodeGenerator synthesis pipeline.

    ```mermaid
    ---
    config:
      flowchart:
        curve: linear
        rankSpacing: 60
        nodeSpacing: 40
    ---
    graph TD

    START([User Query]):::io

    subgraph MEM["Memory Orchestrator  (parallel fetch ≤ 550 tokens)"]
      direction LR
      M1[Reflection Memory]:::mem
      M2[Failure Pattern Hints]:::mem
      M3[User Preferences]:::mem
      M4[Registered Dynamic Tools]:::mem
    end

    SUP{{"Supervisor LLM\n(routing decision)"}}:::supervisor
    HITL_CG{{"HITL Gate\n(interrupt_before)"}}:::hitl
    HITL_AP{{"HITL Gate\n(interrupt_before)"}}:::hitl

    LOGS["Logs Agent\n(ReAct)"]:::agent
    CMS["ConfigMapsSecrets Agent\n(ReAct)"]:::agent
    RBAC["RBAC Agent\n(ReAct)"]:::agent
    MET["Metrics Agent\n(ReAct)"]:::agent
    SEC["Security Agent\n(ReAct)"]:::agent
    LC["Lifecycle Agent\n(ReAct)"]:::agent
    EX["Execution Agent\n(ReAct)"]:::agent
    DEL["Deletion Agent\n(ReAct — conv. confirm)"]:::agent
    INF["Infrastructure Agent\n(ReAct)"]:::agent
    DTE["DynamicToolsExecutor\n(ReAct)"]:::agent
    CG["CodeGenerator\n(synthesis subgraph)"]:::codegen
    AP["Apply Agent\n(YAML apply)"]:::agent

    subgraph DIAG["DiagnosticsOrchestrator  (LangGraph Send API fan-out)"]
      direction TB
      DO["DiagnosticsOrchestrator\n(dispatch node)"]:::diag
      DL["DiagnosticsLogs"]:::diag
      DM["DiagnosticsMetrics"]:::diag
      DE["DiagnosticsEvents"]:::diag
      DC["DiagnosticsCollect\n(barrier sync)"]:::diag
      DO -->|"Send (parallel)"| DL
      DO -->|"Send (parallel)"| DM
      DO -->|"Send (parallel)"| DE
      DL --> DC
      DM --> DC
      DE --> DC
    end

    subgraph CGSUB["CodeGenerator Synthesis Pipeline"]
      direction TB
      GC[generate_code]:::cgnode
      TC[test_code]:::cgnode
      ET{evaluate_test_results}:::cgnode
      GM[generate_metadata]:::cgnode
      RT[register_tool]:::cgnode
      HF[handle_failure]:::cgnode
      FN([finish]):::cgnode
      GC --> TC --> ET
      ET -->|pass| GM
      ET -->|"retry ≤ 3"| GC
      ET -->|max retries| HF
      GM -->|ok| RT
      GM -->|error| HF
      RT --> FN
      HF --> FN
    end

    FINISH([FINISH — stream response]):::io
    OOS([FINISH — out of scope / clarification]):::io

    LF[("Langfuse\n(span per LLM call)")]:::obs
    PROM[("Prometheus\n/metrics")]:::obs

    START --> MEM
    MEM --> SUP
    SUP -->|"route"| LOGS
    SUP -->|"route"| CMS
    SUP -->|"route"| RBAC
    SUP -->|"route"| MET
    SUP -->|"route"| SEC
    SUP -->|"route"| LC
    SUP -->|"route"| EX
    SUP -->|"route"| DEL
    SUP -->|"route"| INF
    SUP -->|"route"| DTE
    SUP -->|"route"| DIAG
    SUP -->|"FINISH\n(task done / OOS / clarification)"| FINISH
    SUP -->|"non-K8s query"| OOS
    SUP -->|"synthesize tool"| HITL_CG
    HITL_CG -->|approve| CG
    HITL_CG -->|deny| FINISH
    SUP -->|"apply YAML"| HITL_AP
    HITL_AP -->|approve| AP
    HITL_AP -->|deny| FINISH
    LOGS --> SUP
    CMS --> SUP
    RBAC --> SUP
    MET --> SUP
    SEC --> SUP
    LC --> SUP
    EX --> SUP
    DEL --> SUP
    INF --> SUP
    DTE --> SUP
    AP --> SUP
    DC --> SUP
    CG --> SUP
    SUP -.->|"LLM span"| LF
    CG -.->|"LLM span"| LF
    SUP -.->|"agent_invocations_total\ntool_calls_total"| PROM

    classDef io fill:#dfe6e9,stroke:#636e72,color:#2d3436,font-weight:bold
    classDef supervisor fill:#6c5ce7,stroke:#4834d4,color:#fff,font-weight:bold
    classDef agent fill:#0984e3,stroke:#0652dd,color:#fff
    classDef codegen fill:#e17055,stroke:#c0392b,color:#fff,font-weight:bold
    classDef cgnode fill:#fdcb6e,stroke:#e17055,color:#2d3436
    classDef diag fill:#00b894,stroke:#00cec9,color:#fff
    classDef hitl fill:#fd79a8,stroke:#e84393,color:#fff,font-weight:bold
    classDef mem fill:#a29bfe,stroke:#6c5ce7,color:#fff
    classDef obs fill:#fff3e0,stroke:#e65100,color:#bf360c
    ```

=== ":material-graph-outline: Full Workflow Topology"

    Complete LangGraph node graph showing every agent, edge, and routing path as
    auto-generated from the compiled StateGraph.

    ```mermaid
    ---
    config:
      flowchart:
        curve: linear
    ---
    graph TD;
        __start__([__start__]):::first

        Supervisor(Supervisor)

        Logs(Logs)
        ConfigMapsSecrets(ConfigMapsSecrets)
        RBAC(RBAC)
        Metrics(Metrics)
        Security(Security)
        Lifecycle(Lifecycle)
        Execution(Execution)
        Deletion(Deletion)
        Infrastructure(Infrastructure)
        DynamicToolsExecutor(DynamicToolsExecutor)
        CodeGenerator(CodeGenerator)
        Apply(Apply)

        DiagnosticsOrchestrator(DiagnosticsOrchestrator)
        DiagnosticsLogs(DiagnosticsLogs)
        DiagnosticsMetrics(DiagnosticsMetrics)
        DiagnosticsEvents(DiagnosticsEvents)
        DiagnosticsCollect(DiagnosticsCollect)

        __end__([__end__]):::last

        __start__ --> Supervisor;
        Supervisor -.-> Logs;
        Supervisor -.-> ConfigMapsSecrets;
        Supervisor -.-> RBAC;
        Supervisor -.-> Metrics;
        Supervisor -.-> Security;
        Supervisor -.-> Lifecycle;
        Supervisor -.-> Execution;
        Supervisor -.-> Deletion;
        Supervisor -.-> Infrastructure;
        Supervisor -.-> DynamicToolsExecutor;
        Supervisor -.-> CodeGenerator;
        Supervisor -.-> Apply;
        Supervisor -.-> DiagnosticsOrchestrator;
        Supervisor -.-> __end__;
        Logs --> Supervisor;
        ConfigMapsSecrets --> Supervisor;
        RBAC --> Supervisor;
        Metrics --> Supervisor;
        Security --> Supervisor;
        Lifecycle --> Supervisor;
        Execution --> Supervisor;
        Deletion --> Supervisor;
        Infrastructure --> Supervisor;
        DynamicToolsExecutor --> Supervisor;
        CodeGenerator --> Supervisor;
        Apply --> Supervisor;
        DiagnosticsOrchestrator -->|"Send"| DiagnosticsLogs;
        DiagnosticsOrchestrator -->|"Send"| DiagnosticsMetrics;
        DiagnosticsOrchestrator -->|"Send"| DiagnosticsEvents;
        DiagnosticsLogs --> DiagnosticsCollect;
        DiagnosticsMetrics --> DiagnosticsCollect;
        DiagnosticsEvents --> DiagnosticsCollect;
        DiagnosticsCollect --> Supervisor;

        classDef default fill:#f2f0ff,stroke:#6c5ce7,color:#2d3436
        classDef first fill-opacity:0,stroke:#636e72
        classDef last fill:#bfb6fc,stroke:#6c5ce7,color:#2d3436,font-weight:bold
    ```

=== ":material-cog-transfer: CodeGenerator Pipeline"

    Internal synthesis subgraph: code generation → sandbox test → evaluate → metadata →
    register. Includes retry loop (≤ 3 attempts) and failure handling path.

    ```mermaid
    ---
    config:
      flowchart:
        curve: linear
    ---
    graph TD;
        __start__([start]):::first
        generate_code(generate_code)
        test_code(test_code)
        evaluate_test_results{evaluate_test_results}
        generate_metadata(generate_metadata)
        register_tool(register_tool)
        handle_failure(handle_failure)
        finish(finish)
        __end__([end]):::last

        __start__ --> generate_code;
        finish --> __end__;
        generate_code --> test_code;
        handle_failure --> finish;
        register_tool --> finish;
        test_code --> evaluate_test_results;
        evaluate_test_results -.->|pass| generate_metadata;
        evaluate_test_results -.->|"retry ≤ 3"| generate_code;
        evaluate_test_results -.->|max retries| handle_failure;
        generate_metadata -.->|ok| register_tool;
        generate_metadata -.->|error| handle_failure;

        classDef default fill:#fae5d3,stroke:#ca6f1e,color:#6e2c00
        classDef first fill-opacity:0,stroke:#636e72
        classDef last fill:#fdcb6e,stroke:#e17055,color:#2d3436,font-weight:bold
    ```

---

## Diagram Source Files

The `.mermaid` source files live in `docs/flowcharts/` and can be rendered with any
Mermaid-compatible tool (VS Code extension, Mermaid Live Editor, etc.).

| File | Contents |
|------|---------|
| [`system-architecture.mermaid`](system-architecture.mermaid) | Full system LR diagram (paper Figure 1) |
| [`supervisor-routing-flow.mermaid`](supervisor-routing-flow.mermaid) | Supervisor TD flow (paper Figure 2) |
| [`full-workflow.mermaid`](full-workflow.mermaid) | Auto-generated LangGraph topology |
| [`supervisor-workflow.mermaid`](supervisor-workflow.mermaid) | Supervisor subgraph detail |
| [`multi-agent-topology.mermaid`](multi-agent-topology.mermaid) | V1 compiled topology reference |
| [`codegenerator-synthesis-pipeline.mermaid`](codegenerator-synthesis-pipeline.mermaid) | CodeGenerator internal subgraph |

!!! tip "Static export"
    The `docs/flowcharts/architectural_diagram.svg` and `.pdf` files are static exports
    suitable for papers, presentations, and offline use.
