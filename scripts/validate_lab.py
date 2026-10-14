"""Validate only this repository's demo lab and write its own report."""

from pathlib import Path
from aks_validation import AksLab
from lab_validation import main

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = AksLab(
    ROOT, "azure-kubernetes-agentic-ops-poc", "rg-agentic-ops-poc-lab", "scripts"
)

if __name__ == "__main__":
    raise SystemExit(main(ADAPTER))
