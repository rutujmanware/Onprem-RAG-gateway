import torch
from sentence_transformers import SentenceTransformer

# Import the model architecture
from model import RouterMLP

class QueryRouter:
    def __init__(self, model_path='best_router_model.pth', threshold=0.5):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.threshold = threshold
        
        self.embedder = SentenceTransformer('all-MiniLM-L6-v2')
        
        self.model = RouterMLP(input_dim=384)
        self.model.load_state_dict(torch.load(model_path, map_location=self.device, weights_only=True))
        self.model.to(self.device)
        self.model.eval() 

    def predict(self, query_text: str) -> dict:
        with torch.no_grad():
            embedding = self.embedder.encode(query_text)
            input_tensor = torch.FloatTensor(embedding).unsqueeze(0).to(self.device)
            
            logit = self.model(input_tensor)
            prob = torch.sigmoid(logit).item()
            
            decision = "hard" if prob >= self.threshold else "easy"
            
            return {
                "query": query_text,
                "route": decision,
                "probability_hard": round(prob, 4)
            }

if __name__ == "__main__":
    # Example usage
    router = QueryRouter(model_path='best_router_model.pth', threshold=0.5)

    # When a new query comes in, just call predict()
    new_query = "What is the capital of France?"
    result = router.predict(new_query)

    print(f"Query: {result['query']}")
    print(f"Routed to: {result['route'].upper()}")
    print(f"Probability of being 'hard': {result['probability_hard']}")