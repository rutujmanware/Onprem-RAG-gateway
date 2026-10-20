import torch.nn as nn

class RouterMLP(nn.Module):
    def __init__(self, input_dim=384):
        super(RouterMLP, self).__init__()
        self.network = nn.Sequential(
            nn.Linear(input_dim, 128),
            nn.BatchNorm1d(128),
            nn.ReLU(),
            nn.Dropout(0.5),

            nn.Linear(128, 64),
            nn.BatchNorm1d(64),
            nn.ReLU(),
            nn.Dropout(0.4),

            nn.Linear(64, 1)
        )

    def forward(self, x):
        return self.network(x).squeeze(-1)