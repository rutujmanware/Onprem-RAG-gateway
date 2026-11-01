import pandas as pd
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader, WeightedRandomSampler
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report
from sentence_transformers import SentenceTransformer
import warnings

# Import the model architecture
from model import RouterMLP

warnings.filterwarnings('ignore')

class RouterDataset(Dataset):
    def __init__(self, X, y):
        self.X = torch.FloatTensor(X)
        self.y = torch.FloatTensor(y)

    def __len__(self):
        return len(self.y)

    def __getitem__(self, idx):
        return self.X[idx], self.y[idx]

def train():
    print("Loading data...")
    df = pd.read_csv('data/router_multichunk_training_dataset.csv')
    df['target'] = df['label'].map({'easy': 0.0, 'hard': 1.0})

    print("Generating text embeddings...")
    embedder = SentenceTransformer('all-MiniLM-L6-v2') 
    X = embedder.encode(df['query'].tolist(), show_progress_bar=True)
    y = df['target'].values

    X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.2, random_state=42, stratify=y)

    train_dataset = RouterDataset(X_train, y_train)
    val_dataset = RouterDataset(X_val, y_val)

    # Class Imbalance Handling
    class_counts = np.bincount(y_train.astype(int))
    class_weights = 1.0 / class_counts
    sample_weights = class_weights[y_train.astype(int)]

    sampler = WeightedRandomSampler(
        weights=torch.DoubleTensor(sample_weights),
        num_samples=len(sample_weights),
        replacement=True
    )

    train_loader = DataLoader(train_dataset, batch_size=256, sampler=sampler)
    val_loader = DataLoader(val_dataset, batch_size=256, shuffle=False)

    # Initialize Model from model.py
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    net = RouterMLP(input_dim=384).to(device)

    criterion = nn.BCEWithLogitsLoss()
    optimizer = torch.optim.Adam(net.parameters(), lr=0.001, weight_decay=1e-3)

    epochs = 15
    patience = 4
    best_val_loss = float('inf')
    epochs_no_improve = 0

    print("\nStarting Training...")
    for epoch in range(epochs):
        net.train()
        train_loss = 0
        
        for batch_X, batch_y in train_loader:
            batch_X, batch_y = batch_X.to(device), batch_y.to(device)
            optimizer.zero_grad()
            outputs = net(batch_X)
            loss = criterion(outputs, batch_y)
            loss.backward()
            optimizer.step()
            train_loss += loss.item()
            
        net.eval()
        val_loss = 0
        with torch.no_grad():
            for batch_X, batch_y in val_loader:
                batch_X, batch_y = batch_X.to(device), batch_y.to(device)
                outputs = net(batch_X)
                loss = criterion(outputs, batch_y)
                val_loss += loss.item()
                
        avg_val_loss = val_loss / len(val_loader)
        print(f"Epoch {epoch+1:02d}/{epochs} | Val Loss: {avg_val_loss:.4f}")
        
        if avg_val_loss < best_val_loss:
            best_val_loss = avg_val_loss
            epochs_no_improve = 0
            torch.save(net.state_dict(), 'best_router_model.pth')
        else:
            epochs_no_improve += 1
            if epochs_no_improve >= patience:
                print("Early stopping triggered.")
                break

if __name__ == "__main__":
    train()