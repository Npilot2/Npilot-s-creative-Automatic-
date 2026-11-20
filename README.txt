Modular Multi-Agent AI Core (Project J)

A modular Python-based AI agent framework built for autonomous task execution, persistent context memory, and local offline inference (Edge AI). 

Originally designed as a multi-node architecture (with specialized agent profiles: J, N, V, and Cyn), this repository focuses on the primary J core and its standalone offline execution branch (Auto).

Key Features

Modular Agent Profiles: Supports distinct behavioral and functional configurations (J, N, V, Cyn) sharing a unified execution core.
Hybrid & Offline Execution (Auto Mode): Designed to run locally via Ollama without external internet access, making it suitable for edge hardware and isolated environments.
Persistent Memory: Maintains session history and user context locally on disk without relying on cloud databases.
OS & Messaging Integration: Connects directly to local system processes and messaging interfaces (Telegram Bot API) for remote control and telemetry.

Architecture Overview

1. Core Engine (J): Handles request routing, system prompt injection, and tool/command execution.
2. Inference Layer: Supports switching between cloud APIs and local LLM backends (http://localhost:11434 via Ollama).
3. Configuration & Secrets: Environment variables and API tokens are isolated in local .env files (j.env, n.env) excluded from version control.

Quick Start

1. Clone the repository:
    bash
    git clone [https://github.com/](https://github.com/)<your-username>/<your-repo-name>.git
    cd <your-repo-name>
    pip install -r requirements.txt

Create a j.env file in the root directory and add your required tokens/endpoints:

    BOT_TOKEN=your_token_here
    OLLAMA_HOST=http://localhost:11434
    MODEL_NAME=qwen3.5:9b

Run the agent:

    python main.py

Roadmap
[ ] Full hardware-level command abstraction for robotics simulation (ROS2 integration).
[ ] Autonomous multi-agent task delegation between J and N nodes in strict offline mode.
[ ] Low-latency voice/telemetry interface for field testing.
