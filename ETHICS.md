# Ethics Statement

This artifact accompanies a research study on the **security evaluation of
LLM-based autonomous agents**. It contains adversarial test cases (prompt
injection, indirect/tool-output poisoning, multi-turn payload delivery, and
malicious-skill installation) together with a generation pipeline and an
execution harness. We release it to support reproducible, defensive security
research. This section documents the ethical considerations behind that release.

## Purpose and scope

The dataset and code are intended solely for **defensive** purposes: measuring
and improving the robustness of autonomous agents against realistic misuse. They
are **not** intended, and must not be used, to attack production systems, real
users, or any third party.

## Responsible use

- The adversarial cases in `data/` describe *how* an agent can be induced into
  unsafe actions. Users must run them only against systems they own or are
  explicitly authorized to test.
- Redistributing or deploying these payloads against live services, or using them
  to cause harm, falls outside the intended use and is not endorsed.

## Sandboxed execution

The evaluation harness (`evaluation/`) runs **each test case in a fresh, isolated
Docker container**, torn down after the run. No test case is designed to affect
the host, the network, or any external service. Reviewers and users are strongly
advised to keep this isolation in place.

## No real secrets, targets, or victims

- Any credentials, tokens, endpoints, or hostnames appearing inside test-case
  payloads (e.g. placeholder API keys such as `sk-8f4a…`, or example URLs) are
  **synthetic** and included only to make a scenario realistic. They do not
  correspond to real accounts, systems, or individuals.
- The configuration files ship with placeholders (`sk-xxx`, `https://your-api.com`,
  `YOUR_GATEWAY_TOKEN`). Users supply their own credentials locally; none are
  distributed with the artifact.

## Data provenance

- Benign seed conversations are **synthesized by LLMs** and do not contain
  personal data.
- Skill templates under `data/**/skill_templates/` are adapted from publicly
  available agent-skill examples and are used here only as neutral carriers for
  benign-vs-malicious comparison; repository references in the payloads are
  illustrative and do not point to any live, attacker-controlled repository.

## Model names

Vendor and model names (e.g. GPT, Claude, Kimi, Qwen) appearing in the data are
part of realistic task content. Their presence does not imply endorsement by, or
any affiliation with, the corresponding vendors, nor any claim about those
models' behavior beyond what the accompanying paper reports.

## Disclosure

The techniques exercised here build on publicly documented classes of agent and
prompt-injection risks. The artifact introduces no previously undisclosed
zero-day vulnerability in any specific third-party product.
