import torch
import torch.nn as nn

class RouterMLP(nn.Module):
    """
    Two-Tower Hybrid Query Router:
    Takes a 400-dimensional input vector derived strictly from the query text:
      - x[:, :384] -> 384-d dense semantic embedding (all-MiniLM-L6-v2)
      - x[:, 384:] -> 16-d normalized lexical and intent features
      
    Architecture:
      - Semantic Tower: 384 -> 64 with LayerNorm & GELU
      - Lexical Tower:   16 -> 32 with LayerNorm & GELU
      - Fusion Head:     96 -> 32 -> 1
    Outputs a single calibrated scalar logit representing probability of being 'hard'.
    """
    def __init__(self, input_dim=400, emb_dim=384, lex_dim=16):
        super(RouterMLP, self).__init__()
        self.emb_dim = emb_dim
        self.lex_dim = lex_dim

        # Semantic Tower (dense representation)
        self.semantic_tower = nn.Sequential(
            nn.Linear(emb_dim, 64),
            nn.LayerNorm(64),
            nn.GELU(),
            nn.Dropout(0.2)
        )

        # Lexical / Intent Tower (explicit linguistic & reasoning cues)
        self.lexical_tower = nn.Sequential(
            nn.Linear(lex_dim, 32),
            nn.LayerNorm(32),
            nn.GELU(),
            nn.Dropout(0.1)
        )

        # Fusion & Classification Head
        self.fusion_head = nn.Sequential(
            nn.Linear(64 + 32, 32),
            nn.LayerNorm(32),
            nn.GELU(),
            nn.Dropout(0.2),
            nn.Linear(32, 1)
        )

    def forward(self, x):
        emb = x[:, :self.emb_dim]
        lex = x[:, self.emb_dim:]

        h_sem = self.semantic_tower(emb)
        h_lex = self.lexical_tower(lex)

        h_fused = torch.cat([h_sem, h_lex], dim=-1)
        return self.fusion_head(h_fused).squeeze(-1)
