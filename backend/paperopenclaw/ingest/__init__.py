"""Ingest pipeline: PDF -> assets -> OKF -> RAG pyramid -> knowledge graph."""

from __future__ import annotations

from .fishbone_builder import FishboneBuilder
from .kg_builder import KnowledgeGraphBuilder
from .okf_writer import OKFWriteResult, assign_asset_sections, write_bundle
from .pdf_layout import CroppedAsset, LayoutReport, analyse_pdf, extract_text_pages
from .pipeline import IngestPipeline
from .pyramid import PyramidBuilder, PyramidResult, extractive_digest
from .vision_parse import ParsedPaper, VisionParser, merge_slices, normalise_payload, reconcile_assets, local_parse

__all__ = [
    "FishboneBuilder",
    "KnowledgeGraphBuilder",
    "OKFWriteResult",
    "assign_asset_sections",
    "write_bundle",
    "CroppedAsset",
    "LayoutReport",
    "analyse_pdf",
    "extract_text_pages",
    "IngestPipeline",
    "PyramidBuilder",
    "PyramidResult",
    "extractive_digest",
    "ParsedPaper",
    "VisionParser",
    "merge_slices",
    "normalise_payload",
    "reconcile_assets",
    "local_parse",
]
