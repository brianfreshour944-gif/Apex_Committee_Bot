# brains/transformer.py — Brain 1 (50% weight): GrokGQA ML Transformer.
# Loads model (grok_gqa_v9_best.pth) & feature scaler (feature_scaler.pkl) directly from Grok v8 architecture.

import os
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import pandas as pd
import joblib

from config import logger, MODEL_PATH, SCALER_PATH, SEQUENCE_LEN
from models import MarketSnapshot, AIDecision
from feature_engineering import add_features, FEATURE_COLS


# ── GrokGQA Transformer architecture (matches Grok v8 exactly) ───────────────────

class GQA_TransformerBlock(nn.Module):
    def __init__(self, embed_dim=128, num_q_heads=8, num_kv_heads=2, dropout=0.1):
        super().__init__()
        self.num_q_heads  = num_q_heads
        self.num_kv_heads = num_kv_heads
        self.head_dim     = embed_dim // num_q_heads
        self.q_proj   = nn.Linear(embed_dim, num_q_heads  * self.head_dim)
        self.k_proj   = nn.Linear(embed_dim, num_kv_heads * self.head_dim)
        self.v_proj   = nn.Linear(embed_dim, num_kv_heads * self.head_dim)
        self.out_proj = nn.Linear(num_q_heads * self.head_dim, embed_dim)
        self.norm1    = nn.LayerNorm(embed_dim)
        self.norm2    = nn.LayerNorm(embed_dim)
        self.ffn      = nn.Sequential(
            nn.Linear(embed_dim, embed_dim * 4),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(embed_dim * 4, embed_dim),
        )
        self.dropout  = nn.Dropout(dropout)

    def forward(self, x):
        residual = x
        norm_x = self.norm1(x)
        batch, seq, _ = norm_x.shape
        
        q = self.q_proj(norm_x).view(batch, seq, self.num_q_heads,  self.head_dim).transpose(1, 2)
        k = self.k_proj(norm_x).view(batch, seq, self.num_kv_heads, self.head_dim).transpose(1, 2)
        v = self.v_proj(norm_x).view(batch, seq, self.num_kv_heads, self.head_dim).transpose(1, 2)
        k = k.repeat_interleave(self.num_q_heads // self.num_kv_heads, dim=1)
        v = v.repeat_interleave(self.num_q_heads // self.num_kv_heads, dim=1)
        
        attn = F.scaled_dot_product_attention(q, k, v)
        attn = attn.transpose(1, 2).contiguous().view(batch, seq, self.num_q_heads * self.head_dim)
        x = residual + self.dropout(self.out_proj(attn))
        
        residual = x
        norm_x = self.norm2(x)
        x = residual + self.ffn(norm_x)
        return x


class GrokGQA_Transformer(nn.Module):
    def __init__(
        self, input_dim=11, seq_len=32,
        embed_dim=128, num_layers=4, num_q_heads=8, num_kv_heads=2, dropout=0.0
    ):
        super().__init__()
        self.input_projection = nn.Linear(input_dim, embed_dim)
        self.pos_encoder      = nn.Parameter(torch.zeros(1, seq_len, embed_dim))
        self.dropout          = nn.Dropout(dropout)
        self.layers           = nn.ModuleList([
            GQA_TransformerBlock(embed_dim, num_q_heads, num_kv_heads, dropout)
            for _ in range(num_layers)
        ])
        self.norm             = nn.LayerNorm(embed_dim)
        self.output_head      = nn.Linear(embed_dim, 1)

    def forward(self, x):
        x = self.input_projection(x)
        x = x + self.pos_encoder 
        x = self.dropout(x)
        for layer in self.layers:
            x = layer(x)
        x = self.norm(x)
        x = self.output_head(x[:, -1, :])
        return x  # raw logit


# ── Transformer Brain Wrapper ──────────────────────────────────────────────────

class TransformerBrain:
    def __init__(self):
        self._model  = None
        self._scaler = None
        self._loaded = False
        self._load()

    def _load(self):
        if not os.path.exists(MODEL_PATH):
            logger.warning(f"⚠️ Transformer model not found at {MODEL_PATH} — brain will SKIP")
            return
        try:
            device = torch.device("cpu")
            model  = GrokGQA_Transformer(input_dim=11, seq_len=SEQUENCE_LEN).to(device)
            model.load_state_dict(torch.load(MODEL_PATH, map_location=device), strict=False)
            model.eval()
            self._model = model

            if os.path.exists(SCALER_PATH):
                self._scaler = joblib.load(SCALER_PATH)
                logger.info(f"✅ Loaded feature scaler from {SCALER_PATH}")
            
            self._loaded = True
            logger.info(f"🤖 Transformer brain loaded successfully from {MODEL_PATH}")
        except Exception as e:
            logger.error(f"Transformer brain load failed: {e}")

    def decide(self, snapshot: MarketSnapshot) -> AIDecision:
        if not self._loaded or self._model is None:
            return AIDecision(
                brain="transformer", action="SKIP", confidence=0.0,
                regime=snapshot.regime, reason="Model not loaded"
            )

        try:
            df = getattr(snapshot, "candles_df", None)
            if df is None or len(df) < SEQUENCE_LEN:
                return AIDecision(
                    brain="transformer", action="SKIP", confidence=0.0,
                    regime=snapshot.regime, reason="Insufficient bars for features"
                )

            # Compute the exact 11 institutional features from Grok v8
            df_feats = add_features(df.copy())
            data = df_feats[FEATURE_COLS].tail(SEQUENCE_LEN).values.astype(np.float32)

            if len(data) < SEQUENCE_LEN:
                return AIDecision(
                    brain="transformer", action="SKIP", confidence=0.0,
                    regime=snapshot.regime, reason="Feature tail length short"
                )

            if self._scaler is not None:
                data = self._scaler.transform(data).astype(np.float32)

            tensor = torch.tensor(data).unsqueeze(0)  # shape (1, 32, 11)

            with torch.no_grad():
                raw_logit = self._model(tensor)
                prob      = torch.sigmoid(raw_logit).item()

            if prob >= 0.52:
                action     = "BUY"
                confidence = prob
            elif prob <= 0.48:
                action     = "SELL"
                confidence = 1.0 - prob
            else:
                action     = "HOLD"
                confidence = abs(prob - 0.50) * 2

            return AIDecision(
                brain="transformer",
                action=action,
                confidence=round(confidence, 4),
                regime=snapshot.regime,
                reason=f"GrokGQA prob={prob:.4f}",
            )

        except Exception as e:
            logger.error(f"Transformer inference failed: {e}")
            return AIDecision(
                brain="transformer", action="SKIP", confidence=0.0,
                regime=snapshot.regime, reason=str(e)
            )


transformer_brain = TransformerBrain()
