import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.distributions import Normal
import torch.optim as optim
import utils

# Sequence model:
#   h_t = f_ϕ(h_{t-1}, z_{t-1}, a_{t-1})
class SequenceModel(nn.Module):
    def __init__(self, latent_size, latent_categories_size, action_size, model_dim, num_blocks=8, device="cpu"):
        super().__init__()
        self.latent_size = latent_size
        self.action_size = action_size
        self.input_size = latent_size * latent_categories_size + action_size
        self.model_dim = model_dim
        self.num_blocks = num_blocks
        self.latent_categories_size = latent_categories_size
        self.device = device
        
        # Each block has hidden_size = D, and we have 8 of these
        self.blocks = nn.ModuleList([
            nn.GRU(
                input_size=self.input_size,
                hidden_size=model_dim,
                batch_first=True
            )
            for _ in range(num_blocks)
        ])
    
    def get_default_hidden(self, batch_size=1):
        return torch.zeros(batch_size, self.model_dim * self.num_blocks, device=self.device)
        #return [None] * self.num_blocks # TODO: The initialization of h as [None] * self.num_blocks in the absence of hidden states may cause issues if the GRU expects tensor inputs. Explicitly initialize h with tensors of appropriate dimensions.
    
    def forward(self, z, a, h):
        """
        z: (batch_size, seq_len, latent_size * latent_categories_size)
        a: (batch_size, seq_len, action_size)
        h: (batch_size, model_dim * num_blocks)
        """
        assert z.size(0) == a.size(0), "Batch size of z and a must match"
        assert z.size(1) == a.size(1), "Sequence length of z and a must match"
        assert z.size(2) == self.latent_size * self.latent_categories_size, "Size[-1] of z must match model's latent size"
        assert a.size(2) == self.action_size, "Size[-1] of a must match model's action size"

        # Combine a and z
        x = torch.cat((a, z), dim=-1)  # shape: (batch_size, seq_len, a_dim + z_dim)

             
        
        batch_size = z.shape[0]
        h = h.view(batch_size, self.num_blocks, self.model_dim)
        h = h.permute(1, 0, 2)

        if self.device.type == "cuda":
            h = h.contiguous() 

        outputs = []        #TODO: check what happens if seq_len is not 1
        #new_h = []
        #Forward pass through each GRU block
        for i, block in enumerate(self.blocks):
            out_i, h_i = block(x, h[i:i+1])
            outputs.append(out_i)
            #new_h.append(h_i)
        
        # Concatenate all the block outputs along the last dimension
        # out_i: (batch_size, seq_len, model_dim)
        # concatenated: (batch_size, seq_len, num_blocks * model_dim)
        combined_output = torch.cat(outputs, dim=-1)            # TODO: Check if this is correct and find out how I can use it simply (get back h_t)
                                                                # TODO: If any GRU changes the sequence length due to different handling of padding or inputs, this will fail
        
        return combined_output#, new_h
    
# Encoder:
#   z_t ~ q_ϕ(z_t | h_t, x_t)
class Encoder(nn.Module):
    def __init__(self, recurrent_size, obs_size, latent_size, latent_categories_size, model_dim):
        super().__init__()
        self.recurrent_size = recurrent_size
        self.obs_size = obs_size
        self.latent_size = latent_size
        self.model_dim = model_dim
        self.latent_categories_size = latent_categories_size
        
        self.model = nn.Sequential(
            nn.Linear(obs_size + recurrent_size, model_dim),
            nn.ReLU(),
            nn.Linear(model_dim, latent_size * latent_categories_size)
        )

    def forward(self, h, x):
        """
        x: (batch_size, obs_dim)
        h: (batch_size, model_dim * num_blocks)
        """
        _x = torch.cat((h, x), dim=-1)
        # Forward pass through the MLP
        logits = self.model(_x).view(-1, self.latent_size, self.latent_categories_size)
        # Apply softmax to get probabilities
        probs = F.softmax(logits, dim=-1)       # TODO: Check dimensions
        
        # Sample from the categorical distribution
        dist = torch.distributions.Categorical(probs=probs)
        sampled = dist.sample()  # Sample indices from the categorical distribution
        hard_sampled = F.one_hot(sampled, num_classes=self.latent_categories_size).float()  # TODO: this assumes that sampled is integer tensor

        # Straight-through trick
        sampled_straight = hard_sampled + probs - probs.detach()
        return sampled_straight, probs
    
# Dynamics predictor:
#   ẑ_t ~ p_ϕ(ẑ_t | h_t)
class DynamicsPredictor(nn.Module):
    def __init__(self, recurrent_size, latent_size, latent_categories_size, model_dim):
        super().__init__()
        self.recurrent_size = recurrent_size
        self.latent_size = latent_size
        self.model_dim = model_dim
        self.latent_categories_size = latent_categories_size
        
        self.model = nn.Sequential(
            nn.Linear(recurrent_size, model_dim),
            nn.ReLU(),
            nn.Linear(model_dim, latent_size * latent_categories_size)
        )

    def forward(self, h):
        # Forward pass through the MLP
        logits = self.model(h).view(-1, self.latent_size, self.latent_categories_size)
        # Apply softmax to get probabilities
        probs = F.softmax(logits, dim=-1)       # TODO: Check dimensions
        
        # Sample from the categorical distribution
        dist = torch.distributions.Categorical(probs=probs)
        sampled = dist.sample()  # Sample indices from the categorical distribution
        hard_sampled = F.one_hot(sampled, num_classes=self.latent_categories_size).float()

        # Straight-through trick
        sampled_straight = hard_sampled + probs - probs.detach()
        return sampled_straight, probs

# r̂_t ~ p_ϕ(r̂_t | h_t, z_t)
class RewardPredictor(nn.Module):   # TODO: initially predicts 0
    def __init__(self, hidden_dim, latent_dim, embedding_dim, output_dim, model_dim, bins):
        """
        Parameters:
        - hidden_dim: Dimension of the recurrent hidden state h_t
        - latent_dim: Number of categorical latent variables (each with its own classes)
        - embedding_dim: Embedding size for each categorical variable
        - output_dim: Dimension of the predicted output (e.g., reward distribution)
        - model_dim: Number of units in each hidden layer
        """
        super().__init__()

        self.bins = bins

        self.latent_dim = latent_dim

        # Input dimension is hidden state + all embedded latent dimensions
        input_dim = hidden_dim + embedding_dim * latent_dim

        self.model = nn.Sequential(
            nn.Linear(input_dim, model_dim),
            nn.ReLU(),
            nn.Linear(model_dim, output_dim)
        )

    def forward(self, h, z,):    # TODO: Check if z is a list of tensors
        """
        h: Tensor of shape (batch_size, hidden_dim)
        z: Tensor of shape (batch_size, latent_dim * embedding_dim)
        """
 
        # Concatenate h with embedded latent variables
        input_tensor = torch.cat([h, z], dim=-1)
        r = self.model(input_tensor)
        return r

# Continue predictor:
# ĉ_t ~ p_ϕ(ĉ_t | h_t, z_t)
class ContinuePredictor(nn.Module):
    def __init__(self, hidden_dim, latent_dim, embedding_dim, output_dim, model_dim):
        """
        Parameters:
        - hidden_dim: Dimension of the recurrent hidden state h_t
        - latent_dim: Number of categorical latent variables (each with its own classes)
        - embedding_dim: Embedding size for each categorical variable
        - output_dim: Dimension of the predicted output (e.g., reward distribution)
        - model_dim: Number of units in each hidden layer
        """
        super().__init__()

        self.latent_dim = latent_dim

        # Input dimension is hidden state + all embedded latent dimensions
        input_dim = hidden_dim + embedding_dim * latent_dim

        self.model = nn.Sequential(
            nn.Linear(input_dim, model_dim),
            nn.ReLU(),
            nn.Linear(model_dim, output_dim),
            nn.Sigmoid()
        )

    def forward(self, h, z):    # TODO: Check if z is a list of tensors
        """
        h: Tensor of shape (batch_size, hidden_dim)
        z: Tensor of shape (batch_size, latent_dim * embedding_dim)
        """
    
        # Concatenate h with embedded latent variables
        input_tensor = torch.cat([h, z], dim=-1)
        return self.model(input_tensor)


class Decoder(nn.Module):
    def __init__(self, hidden_dim, latent_dim, embedding_dim, output_dim, model_dim):
        """
        Parameters:
        - hidden_dim: Dimension of the recurrent hidden state h_t
        - latent_dim: Number of categorical latent variables (each with its own classes)
        - embedding_dim: Embedding size for each categorical variable
        - output_dim: Dimension of the predicted output (e.g., reward distribution)
        - model_dim: Number of units in each hidden layer
        """
        super().__init__()
        self.hidden_dim = hidden_dim
        self.latent_dim = latent_dim
        self.embedding_dim = embedding_dim
        self.output_dim = output_dim
        self.model_dim = model_dim
        
        input_dim = hidden_dim + embedding_dim * latent_dim

        self.model = nn.Sequential(
            nn.Linear(input_dim, model_dim),
            nn.ReLU(),
            nn.Linear(model_dim, output_dim),
        )

    def forward(self, h, z):
        """
        h: Tensor of shape (batch_size, hidden_dim)
        z: Tensor of shape (batch_size, latent_dim * embedding_dim)
        """
        # Concatenate h with embedded latent variables
        input_tensor = torch.cat([h, z], dim=-1)
        return self.model(input_tensor)
    
class WorldModel():
    def __init__(self, latent_dim, action_dim, obs_size, latent_categories_size, model_dim, bins, min_reward, max_reward, num_blocks=8, device='cpu'):
        super().__init__()
        self.latent_dim = latent_dim
        self.action_dim = action_dim
        self.obs_size = obs_size
        self.latent_categories_size = latent_categories_size
        self.model_dim = model_dim
        self.bins = bins
        self.num_blocks = num_blocks
        self.device = device
        self.min_reward = min_reward
        self.max_reward = max_reward
        
        
        self.sequence_model = SequenceModel(latent_dim, latent_categories_size, action_dim, model_dim, num_blocks, device).to(device)
        self.encoder = Encoder(model_dim * num_blocks, obs_size, latent_dim, latent_categories_size, model_dim).to(device)
        self.dynamics_predictor = DynamicsPredictor(model_dim * num_blocks, latent_dim, latent_categories_size, model_dim).to(device)
        self.reward_predictor = RewardPredictor(model_dim * num_blocks, latent_dim, latent_categories_size, 1, model_dim, bins).to(device)
        self.continue_predictor = ContinuePredictor(model_dim * num_blocks, latent_dim, latent_categories_size, 1, model_dim).to(device)
        self.decoder = Decoder(model_dim * num_blocks, latent_dim, latent_categories_size, obs_size, model_dim).to(device)

        params = list(self.sequence_model.parameters()) + \
                list(self.encoder.parameters()) + \
                list(self.dynamics_predictor.parameters()) + \
                list(self.reward_predictor.parameters()) + \
                list(self.continue_predictor.parameters()) + \
                list(self.decoder.parameters())
                
        self.optimizer = optim.Adam(params, lr=4e-3)

    def get_latent(self, h, x):
        """
        h = (model_dim * num_blocks)
        x = (obs_size)
        returns: z = (1, latent_dim, latent_categories_size)
        """
        z, _ = self.encoder(h, x.unsqueeze(dim=0))
        return z
    def get_recurrent_hidden(self, h, z, a):
        """
        h = (1, model_dim * num_blocks)
        z = (1, latent_dim, latent_categories_size)
        a = (action_dim)
        """
        h = h.view(1,1,-1)
        z = z.view(1,1,-1)
        a = a.view(1,1,-1)

        new_h = self.sequence_model(z, a, h) # (batch_size, seq_len, num_blocks * model_dim), but seq_len is 1
        new_h = new_h.squeeze(dim=1)
        return new_h
    
    def imagine(self, z_0, actor, imag_horizon):
        '''
        
        '''
        h = []
        z = []
        r = []
        c = []
        a_all_probs = []
        a_taken_probs = []
        #TODO: this is only pseudocode almost, need to check dimensions
        #TODO: do we need probs or just the sampled values?
        h_t = self.get_default_hidden(z_0.shape[0])
        z_t = z_0
        for _ in range(imag_horizon):
            z_t_long = z_t.view(-1, self.latent_dim * self.latent_categories_size)  # (batch_size, latent_dim * latent_categories_size)
            a_t_cat, a_t_all_probs = actor(h_t, z_t_long)
            a_t_index = torch.argmax(a_t_cat, dim=-1, keepdim=True)
            a_t_taken_probs = torch.gather(a_t_all_probs, dim=-1, index=a_t_index)  
            a_t_cat = a_t_cat.view(-1, self.action_dim, self.bins)  # (batch_size, action_dim, action_bins)

            r_t = self.reward_predictor(h_t, z_t_long) # (batch_size, 1)
            c_t_cat = self.continue_predictor(h_t, z_t_long)                # (batch_size, 1)

            a_t = utils.get_value_from_distribution(a_t_cat, -1, 1)     # (batch_size, action_dim)
            c_t = (c_t_cat > 0.5).float().to(self.device)               # (batch_size, 1)

            h.append(h_t)
            z.append(z_t_long)
            a_all_probs.append(a_t_all_probs)
            a_taken_probs.append(a_t_taken_probs)
            r.append(r_t)
            c.append(c_t)

            h_new = self.sequence_model(z_t_long.unsqueeze(dim=1), a_t.unsqueeze(dim=1), h_t.unsqueeze(dim=1))
            z_new, _ = self.dynamics_predictor(h_new)

            h_t = h_new.view(-1, self.model_dim * self.num_blocks)
            z_t = z_new
        h.append(h_t)
        z.append(z_t.view(-1, self.latent_dim * self.latent_categories_size))
        
        h = torch.stack(h, dim=1) # (batch_size, imag_horizon + 1, recurrent_hidden_dim * num_blocks)
        z = torch.stack(z, dim=1) # (batch_size, imag_horizon + 1, latent_dim * latent_categories_size)
        a_all_probs = torch.stack(a_all_probs, dim=1) # (batch_size, imag_horizon, action_dim, bins)
        a_taken_probs = torch.stack(a_taken_probs, dim=1).squeeze(dim=-1) # (batch_size, imag_horizon, action_dim)
        r = torch.stack(r, dim=1) # (batch_size, imag_horizon, 1)
        c = torch.stack(c, dim=1) # (batch_size, imag_horizon, 1)

        
        return h, z, a_all_probs, a_taken_probs, r, c
    
    def get_default_hidden(self, batch_size=1):
        return self.sequence_model.get_default_hidden(batch_size)

    def train(self, x, a, r, c, z_memory):
        """
        x: (batch_size, seq_len, obs_size)
        a: (batch_size, seq_len, action_dim)
        r: (batch_size, seq_len)
        c: (batch_size, seq_len)
        z_memory: (batch_size, seq_len, latent_dim, latent_categories_size)
        """
        batch_size = x.shape[0]
        seq_len = x.shape[1]
        # Train the world model
        l1_loss = nn.L1Loss()
        mse_loss = nn.MSELoss()
        kl_loss = nn.KLDivLoss(reduction="batchmean", log_target=True)

        
        # Calculate the hidden states from the old latents, and the new latents
        h_0 = self.get_default_hidden(batch_size)

        h = self.sequence_model(z_memory.view(batch_size, seq_len, -1), a, h_0).squeeze(dim=1)
        h = torch.cat([h_0.unsqueeze(dim=1), h[:,:-1]], dim=1)
        h = h.view(batch_size * seq_len, -1)
         # (batch_size * seq_len, latent_dim, latent_categories_size)
        z_new, z_prob = self.encoder(h, x.view(batch_size * seq_len, -1))
        
        z_new_long = z_new.view(batch_size * seq_len, -1)
        x = x.view(batch_size * seq_len, -1)
        r = r.view(batch_size * seq_len, 1)
        c = c.view(batch_size * seq_len, 1)
        # L_pred
        x_out = self.decoder(h, z_new_long)
        r_out = self.reward_predictor(h, z_new_long)
        #c_out = self.continue_predictor(h, z_new_long)
        loss_pred = mse_loss(x_out,x) + l1_loss(r_out,r) #+ F.cross_entropy(c_out, c)

        # L_dyn + L_enc
        # already computed
        # z_new, z_prob = self.encoder(h, x)
        _, z_dyn_prob = self.dynamics_predictor(h)

        z_prob = torch.log(z_prob * 0.99 + 0.01 / self.latent_categories_size)
        z_dyn_prob = torch.log(z_dyn_prob * 0.99 + 0.01 / self.latent_categories_size)

        loss_dyn = torch.clamp(kl_loss(z_dyn_prob.view(-1, self.latent_categories_size), z_prob.detach().view(-1, self.latent_categories_size)), max=1)     # have to switch positions
        loss_enc = torch.clamp(kl_loss(z_dyn_prob.detach().view(-1, self.latent_categories_size), z_prob.view(-1, self.latent_categories_size)), max=1)
        # L_total

        loss_total = loss_pred + loss_dyn + 0.1 * loss_enc
        

        self.optimizer.zero_grad()
        loss_total.backward()
        self.optimizer.step()
        #print(loss_total.item())
        return (loss_pred, loss_dyn, 0.1 * loss_enc), z_new.view(batch_size, seq_len, self.latent_dim, self.latent_categories_size)
        






