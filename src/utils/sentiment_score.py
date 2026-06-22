import os
import re
import warnings
from typing import List
from datetime import datetime

import pandas as pd
import torch
from tqdm import tqdm
from transformers import (
    AutoModelForSequenceClassification,
    AutoTokenizer,
    logging as hf_logging,
)

# Silence noisy HF warnings
hf_logging.set_verbosity_error()
warnings.filterwarnings("ignore", category=FutureWarning)

# ───────────────────────────────────────────
# Constants
# ───────────────────────────────────────────

STANCEBERTA = "eevvgg/StanceBERTa"

DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# StanceBERTa label map  {0: neutral, 1: positive, 2: negative}

# ───────────────────────────────────────────
# Text preprocessing
# ───────────────────────────────────────────

def preprocess_for_stance(text: str) -> str:
    """StanceBERTa was trained with @user and http tokens."""
    text = re.sub(r"@\w+", "@user", text)
    text = re.sub(r"https?://\S+|www\.\S+", "http", text)
    return text.strip()


def safe_text(text) -> str:
    """Guarantee a non-empty string for the tokeniser."""
    if not isinstance(text, str) or not text.strip():
        return "."
    return text.strip()


# ───────────────────────────────────────────
# Scorer class
# ───────────────────────────────────────────

class SentimentScorer:
    """Load a HF model once and batch-score texts into [-1, +1]."""

    def __init__(self, model_name: str, max_length: int = 512):
        print(f"Loading {model_name} …")
        self.model_name = model_name
        self.max_length = max_length
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_name)
        self.model.to(DEVICE).eval()

        self._preprocess = (
            preprocess_for_stance if "stance" in model_name.lower() else lambda t: t
        )

    # ── single text (handy for debugging) ─────────────

    def score_one(self, text: str) -> float:
        text = self._preprocess(safe_text(text))
        inputs = self.tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            padding=True,
            max_length=self.max_length,
        ).to(DEVICE)
        with torch.no_grad():
            probs = (
                torch.nn.functional.softmax(self.model(**inputs).logits, dim=-1)
                .squeeze()
                .cpu()
                .tolist()
            )
        return self.stance_probs_to_score(probs)

    # ── batch scoring ─────────────────────────────────

    def score_batch(self, texts: List[str], batch_size: int = 32) -> List[float]:
        texts = [self._preprocess(safe_text(t)) for t in texts]
        scores: List[float] = []

        for start in tqdm(
            range(0, len(texts), batch_size),
            desc=self.model_name.split("/")[-1],
            leave=False,
        ):
            batch = texts[start : start + batch_size]
            inputs = self.tokenizer(
                batch,
                return_tensors="pt",
                truncation=True,
                padding=True,
                max_length=self.max_length,
            ).to(DEVICE)

            with torch.no_grad():
                logits = self.model(**inputs).logits
                probs = torch.nn.functional.softmax(logits, dim=-1).cpu().tolist()

            for prob_row in probs:
                scores.append(self.stance_probs_to_score(prob_row))

        return scores

    def stance_probs_to_score(self, probs: list) -> float:
        """[+1 × P(pos) + -1 × P(neg)] * [1 − 1 × P(neu)]"""
        if "stance" in self.model_name.lower():
            return (probs[1] - probs[2]) * (1 - probs[0])
        else:
            return 0

# ───────────────────────────────────────────
# Main
# ───────────────────────────────────────────

def process(models: list[str] = [STANCEBERTA], input_file: str = "./datasets/sentiment/sentiment_2026_03_20_22_56.csv", output_dir: str = "./datasets/scores", filename: str = "scored_sentiment.csv"):
    # ── load data ───────────────────────────
    df = pd.read_csv(input_file)
    print(f"Loaded {len(df)} rows from {input_file}")

    df = df.dropna(subset=["text_cleaned"])
    df = df[df["text_cleaned"].str.strip().astype(bool)].copy()
    df.reset_index(drop=True, inplace=True)
    print(f"{len(df)} rows after dropping empty text")

    texts = df["text_cleaned"].tolist()

    # ── score with each requested model ─────
    score_cols: List[str] = []

    for model in models:
        scorer = SentimentScorer(model)
        df[f"{model}_score"] = scorer.score_batch(texts)
        score_cols.append(f"{model}_score")
        del scorer
        torch.cuda.empty_cache()

    # ── ensemble ────────────────────────────
    if score_cols:
        df["sentiment"] = df[score_cols].mean(axis=1).round(6)

    # ── save ────────────────────────────────
    out_path = os.path.join(output_dir, filename) or "./datasets/scores/scored_sentiment.csv"
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    df.to_csv(out_path, index=False)
    print(f"\nSaved {len(df)} scored rows → {out_path}")

    # Quick summary
    print("\n── Score summary ──")
    for col in score_cols + ["sentiment"]:
        if col in df.columns:
            print(f"  {col:>15s}  mean={df[col].mean():.4f}  std={df[col].std():.4f}")

    if "source" in df.columns:
        print("\n── By source ──")
        print(df.groupby("source")["sentiment"].agg(["mean", "std", "count"]).to_string())


if __name__ == "__main__":
    DATE = datetime.now().strftime("%Y_%m_%d")
    FILENAME = f'scored_sentiment_{DATE}.csv'
    process(input_file="./datasets/sentiment/sentiment_2026_03_21.csv", filename=FILENAME)