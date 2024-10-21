import math
import time
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch_scatter import scatter_sum, scatter_softmax, scatter_mean

def nan_to_num(tensor, nan=0.0):
    idx = torch.isnan(tensor)
    tensor[idx] = nan
    return tensor

def _normalize(tensor, dim=-1):
    return nan_to_num(tensor / (torch.norm(tensor, dim=dim, keepdim=True) + 1e-9))

def _quaternions(R):
    diag = torch.diagonal(R, dim1=-2, dim2=-1)
    Rxx, Ryy, Rzz = diag.unbind(-1)
    magnitudes = 0.5 * torch.sqrt(torch.abs(1 + torch.stack([
            Rxx - Ryy - Rzz, 
        - Rxx + Ryy - Rzz, 
        - Rxx - Ryy + Rzz
    ], -1)) + 1e-9)
    _R = lambda i,j: R[:,:,:,i,j]
    signs = torch.sign(torch.stack([
        _R(2,1) - _R(1,2),
        _R(0,2) - _R(2,0),
        _R(1,0) - _R(0,1)
    ], -1))
    xyz = signs * magnitudes
    w = torch.sqrt(F.relu(1 + diag.sum(-1, keepdim=True))) / 2.
    Q = torch.cat((xyz, w), -1)
    return _normalize(Q, dim=-1)


def cal_dihedral(X, eps=1e-7):
    dX = X[:,1:,:] - X[:,:-1,:] # CA-N, C-CA, N-C, CA-N...
    U = _normalize(dX, dim=-1)
    u_0 = U[:,:-2,:] # CA-N, C-CA, N-C,...
    u_1 = U[:,1:-1,:] # C-CA, N-C, CA-N, ... 0, psi_{i}, omega_{i}, phi_{i+1} or 0, tau_{i},...
    u_2 = U[:,2:,:] # N-C, CA-N, C-CA, ...

    n_0 = _normalize(torch.linalg.cross(u_0, u_1), dim=-1)
    n_1 = _normalize(torch.linalg.cross(u_1, u_2), dim=-1)
    
    cosD = (n_0 * n_1).sum(-1)
    cosD = torch.clamp(cosD, -1+eps, 1-eps)
    
    v = _normalize(torch.linalg.cross(n_0, n_1), dim=-1)
    D = torch.sign((-v* u_1).sum(-1)) * torch.acos(cosD) # TODO: sign
    
    return D


def _orientations_coarse_gl_tuple(X, E_idx, eps=1e-6):
    V = X.clone()
    X = X[:,:,:3,:].reshape(X.shape[0], 3*X.shape[1], 3) 
    dX = X[:,1:,:] - X[:,:-1,:] # CA-N, C-CA, N-C, CA-N...
    U = _normalize(dX, dim=-1)
    u_0, u_1 = U[:,:-2,:], U[:,1:-1,:]
    n_0 = _normalize(torch.linalg.cross(u_0, u_1), dim=-1)
    b_1 = _normalize(u_0 - u_1, dim=-1)
    
    n_0 = n_0[:,::3,:]
    b_1 = b_1[:,::3,:]
    X = X[:,::3,:]
    Q = torch.stack((b_1, n_0, torch.linalg.cross(b_1, n_0)), 2)
    Q = Q.view(list(Q.shape[:2]) + [9])
    Q = F.pad(Q, (0,0,0,1), 'constant', 0) # [16, 464, 9]

    Q_neighbors = gather_nodes(Q, E_idx) # [16, 464, 30, 9]
    X_neighbors = gather_nodes(V[:,:,1,:], E_idx) # [16, 464, 30, 3]
    N_neighbors = gather_nodes(V[:,:,0,:], E_idx)
    C_neighbors = gather_nodes(V[:,:,2,:], E_idx)
    O_neighbors = gather_nodes(V[:,:,3,:], E_idx)

    Q = Q.view(list(Q.shape[:2]) + [3,3]).unsqueeze(2) # [16, 464, 1, 3, 3]
    Q_neighbors = Q_neighbors.view(list(Q_neighbors.shape[:3]) + [3,3]) # [16, 464, 30, 3, 3]

    dX = torch.stack([X_neighbors,N_neighbors,C_neighbors,O_neighbors], dim=3) - X[:,:,None,None,:] # [16, 464, 30, 3]
    dU = torch.matmul(Q[:,:,:,None,:,:], dX[...,None]).squeeze(-1) # [16, 464, 30, 3] 邻居的相对坐标
    B, N, K = dU.shape[:3]
    E_direct = _normalize(dU, dim=-1)
    E_direct = E_direct.reshape(B, N, K,-1)
    R = torch.matmul(Q.transpose(-1,-2), Q_neighbors)
    q = _quaternions(R)
    # edge_feat = torch.cat((dU, q), dim=-1) # 相对方向向量+旋转四元数
    
    dX_inner = V[:,:,[0,2,3],:] - X.unsqueeze(-2)
    dU_inner = torch.matmul(Q, dX_inner.unsqueeze(-1)).squeeze(-1)
    dU_inner = _normalize(dU_inner, dim=-1)
    V_direct = dU_inner.reshape(B,N,-1)
    return V_direct, E_direct, q
    


def _rbf(D, num_rbf):
    D_min, D_max, D_count = 0., 20., num_rbf
    D_mu = torch.linspace(D_min, D_max, D_count).to(D.device)
    D_mu = D_mu.view([1,1,1,-1])
    D_sigma = (D_max - D_min) / D_count
    D_expand = torch.unsqueeze(D, -1)
    RBF = torch.exp(-((D_expand - D_mu) / D_sigma)**2)
    return RBF

def _get_rbf(A, B, E_idx=None, num_rbf=16):
    if E_idx is not None:
        D_A_B = torch.sqrt(torch.sum((A[:,:,None,:] - B[:,None,:,:])**2,-1) + 1e-6) #[B, L, L]
        D_A_B_neighbors = gather_edges(D_A_B[:,:,:,None], E_idx)[:,:,:,0] #[B,L,K]
        RBF_A_B = _rbf(D_A_B_neighbors, num_rbf)
    else:
        D_A_B = torch.sqrt(torch.sum((A[:,:,None,:] - B[:,:,None,:])**2,-1) + 1e-6) #[B, L, L]
        RBF_A_B = _rbf(D_A_B, num_rbf)
    return RBF_A_B


def _dihedrals(X, dihedral_type=0, eps=1e-7):
    B, N, _, _ = X.shape
    # psi, omega, phi
    X = X[:,:,:3,:].reshape(X.shape[0], 3*X.shape[1], 3) # ['N', 'CA', 'C', 'O']
    D = cal_dihedral(X)
    D = F.pad(D, (1,2), 'constant', 0)
    D = D.view((D.size(0), int(D.size(1)/3), 3)) 
    Dihedral_Angle_features = torch.cat((torch.cos(D), torch.sin(D)), 2)

    # alpha, beta, gamma
    dX = X[:,1:,:] - X[:,:-1,:] # CA-N, C-CA, N-C, CA-N...
    U = _normalize(dX, dim=-1)
    u_0 = U[:,:-2,:] # CA-N, C-CA, N-C,...
    u_1 = U[:,1:-1,:] # C-CA, N-C, CA-N, ...
    cosD = (u_0*u_1).sum(-1) # alpha_{i}, gamma_{i}, beta_{i+1}
    cosD = torch.clamp(cosD, -1+eps, 1-eps)
    D = torch.acos(cosD)
    D = F.pad(D, (1,2), 'constant', 0)
    D = D.view((D.size(0), int(D.size(1)/3), 3))
    Angle_features = torch.cat((torch.cos(D), torch.sin(D)), 2)

    D_features = torch.cat((Dihedral_Angle_features, Angle_features), 2)
    return D_features


def gather_edges(edges, neighbor_idx):
    neighbors = neighbor_idx.unsqueeze(-1).expand(-1, -1, -1, edges.size(-1))
    return torch.gather(edges, 2, neighbors)

def gather_nodes(nodes, neighbor_idx):
    neighbors_flat = neighbor_idx.view((neighbor_idx.shape[0], -1)) # [4, 317, 30]-->[4, 9510]
    neighbors_flat = neighbors_flat.unsqueeze(-1).expand(-1, -1, nodes.size(2)) # [4, 9510, dim]
    neighbor_features = torch.gather(nodes, 1, neighbors_flat) # [4, 9510, dim]
    return neighbor_features.view(list(neighbor_idx.shape)[:3] + [-1]) # [4, 317, 30, 128]


def get_attend_mask(idx, mask):
    mask_attend = gather_nodes(mask.unsqueeze(-1), idx).squeeze(-1) # 一阶邻居节点的mask: 1代表节点存在, 0代表节点不存在
    mask_attend = mask.unsqueeze(-1) * mask_attend # 自身的mask*邻居节点的mask
    return mask_attend

#################################### node modules ###############################
class NeighborAttention(nn.Module):
    def __init__(self, num_hidden, num_in, num_heads=4, edge_drop=0.0, output_mlp=True):
        super(NeighborAttention, self).__init__()
        self.num_heads = num_heads
        self.num_hidden = num_hidden
        self.edge_drop = edge_drop
        self.output_mlp = output_mlp
        
        self.W_V = nn.Sequential(nn.Linear(num_in, num_hidden),
                                nn.GELU(),
                                nn.Linear(num_hidden, num_hidden),
                                nn.GELU(),
                                nn.Linear(num_hidden, num_hidden)
        )
        self.Bias = nn.Sequential(
                                nn.Linear(num_hidden*3, num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_heads)
                                )
        self.W_O = nn.Linear(num_hidden, num_hidden, bias=False)

    def forward(self, h_V, h_E, center_id, batch_id, dst_idx=None):
        N = h_V.shape[0]
        E = h_E.shape[0]
        n_heads = self.num_heads
        d = int(self.num_hidden / n_heads)
        
        w = self.Bias(torch.cat([h_V[center_id], h_E],dim=-1)).view(E, n_heads, 1) 
        attend_logits = w/np.sqrt(d) 

        V = self.W_V(h_E).view(-1, n_heads, d) 
        attend = scatter_softmax(attend_logits, index=center_id, dim=0)
        h_V = scatter_sum(attend*V, center_id, dim=0).view([-1, self.num_hidden])

        if self.output_mlp:
            h_V_update = self.W_O(h_V)
        else:
            h_V_update = h_V
        return h_V_update


#################################### edge modules ###############################
class EdgeMLP(nn.Module):
    def __init__(self, num_hidden, num_in, dropout=0.1, num_heads=None, scale=30):
        super(EdgeMLP, self).__init__()
        self.num_hidden = num_hidden
        self.num_in = num_in
        self.scale = scale
        self.dropout = nn.Dropout(dropout)
        self.norm = nn.BatchNorm1d(num_hidden)
        self.W11 = nn.Linear(num_hidden + num_in, num_hidden, bias=True)
        self.W12 = nn.Linear(num_hidden, num_hidden, bias=True)
        self.W13 = nn.Linear(num_hidden, num_hidden, bias=True)
        self.act = torch.nn.GELU()

    def forward(self, h_V, h_E, edge_idx, batch_id):
        src_idx = edge_idx[0]
        dst_idx = edge_idx[1]

        h_EV = torch.cat([h_V[src_idx], h_E, h_V[dst_idx]], dim=-1)
        h_message = self.W13(self.act(self.W12(self.act(self.W11(h_EV)))))
        h_E = self.norm(h_E + self.dropout(h_message))
        return h_E

#################################### context modules ###############################
class Context(nn.Module):
    def __init__(self, num_hidden, num_in, dropout=0.1, num_heads=None, scale=30, node_context = False, edge_context = False):
        super(Context, self).__init__()
        self.num_hidden = num_hidden
        self.num_in = num_in
        self.scale = scale
        self.node_context = node_context
        self.edge_context = edge_context

        self.V_MLP = nn.Sequential(
                                nn.Linear(num_hidden, num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_hidden),
                                )
        
        self.V_MLP_g = nn.Sequential(
                                nn.Linear(num_hidden, num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_hidden),
                                nn.Sigmoid()
                                )

        self.E_MLP = nn.Sequential(
                                nn.Linear(num_hidden, num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_hidden)
                                )
        
        self.E_MLP_g = nn.Sequential(
                                nn.Linear(num_hidden, num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_hidden),
                                nn.ReLU(),
                                nn.Linear(num_hidden,num_hidden),
                                nn.Sigmoid()
                                )

    def forward(self, h_V, h_E, edge_idx, batch_id):
        if self.node_context:
            c_V = scatter_mean(h_V, batch_id, dim=0)
            h_V = h_V * self.V_MLP_g(c_V[batch_id])
        
        if self.edge_context:
            c_V = scatter_mean(h_V, batch_id, dim=0)
            h_E = h_E * self.E_MLP_g(c_V[batch_id[edge_idx[0]]])

        return h_V, h_E


class GeneralGNN(nn.Module):
    def __init__(self, num_hidden, num_in, dropout=0.1, num_heads=None, scale=30, node_net = 'AttMLP', edge_net = 'EdgeMLP', node_context = 0, edge_context = 0):
        super(GeneralGNN, self).__init__()
        self.num_hidden = num_hidden
        self.num_in = num_in
        self.scale = scale
        self.dropout = nn.Dropout(dropout)
        self.norm = nn.ModuleList([nn.BatchNorm1d(num_hidden) for _ in range(3)])
        self.node_net = node_net
        self.edge_net = edge_net
        if node_net == 'AttMLP':
            self.attention = NeighborAttention(num_hidden, num_in, num_heads=4) 
        if edge_net == 'None':
            pass
        if edge_net == 'EdgeMLP':
            self.edge_update = EdgeMLP(num_hidden, num_in, num_heads=4)
        
        self.context = Context(num_hidden, num_in, num_heads=4, node_context=node_context, edge_context=edge_context)

        self.dense = nn.Sequential(
            nn.Linear(num_hidden, num_hidden*4),
            nn.ReLU(),
            nn.Linear(num_hidden*4, num_hidden)
        )
        self.W11 = nn.Linear(num_hidden + num_in, num_hidden, bias=True)
        self.W12 = nn.Linear(num_hidden, num_hidden, bias=True)
        self.W13 = nn.Linear(num_hidden, num_hidden, bias=True)
        self.act = torch.nn.GELU()

    def forward(self, h_V, h_E, edge_idx, batch_id):
        src_idx = edge_idx[0]
        dst_idx = edge_idx[1]

        if self.node_net == 'AttMLP' or self.node_net == 'QKV':
            dh = self.attention(h_V, torch.cat([h_E, h_V[dst_idx]], dim=-1), src_idx, batch_id, dst_idx)
        else:
            dh = self.attention(h_V, h_E, src_idx, batch_id, dst_idx)
        h_V = self.norm[0](h_V + self.dropout(dh))
        dh = self.dense(h_V)
        h_V = self.norm[1](h_V + self.dropout(dh))

        if self.edge_net=='None':
            pass
        else:
            h_E = self.edge_update( h_V, h_E, edge_idx, batch_id )

        h_V, h_E = self.context(h_V, h_E, edge_idx, batch_id)
        return h_V, h_E



class StructureEncoder(nn.Module):
    def __init__(self,  hidden_dim, num_encoder_layers=3, dropout=0, node_net = 'AttMLP', edge_net = 'EdgeMLP', node_context = True, edge_context = False):
        """ Graph labeling network """
        super(StructureEncoder, self).__init__()
        encoder_layers = []
        
        module = GeneralGNN

        for i in range(num_encoder_layers):
            encoder_layers.append(
                module(hidden_dim, hidden_dim*2, dropout=dropout, node_net = node_net, edge_net = edge_net, node_context = node_context, edge_context = edge_context),
            )
        
        
        self.encoder_layers = nn.Sequential(*encoder_layers)

    def forward(self, h_V, h_P, P_idx, batch_id):
        for layer in self.encoder_layers:
            h_V, h_P = layer(h_V, h_P, P_idx, batch_id)
        return h_V, h_P


class MLPDecoder(nn.Module):
    def __init__(self, hidden_dim, vocab=20):
        super().__init__()
        self.readout = nn.Linear(hidden_dim, vocab)
    
    def forward(self, h_V, batch_id=None):
        logits = self.readout(h_V)
        log_probs = F.log_softmax(logits, dim=-1)
        return log_probs, logits


class ProDesign_Model(nn.Module):
    def __init__(self, args,  **kwargs):
        """ Graph labeling network """
        super(ProDesign_Model, self).__init__()
        self.args = args
        node_features = args.node_features
        edge_features = args.edge_features
        hidden_dim = args.hidden_dim
        dropout = args.dropout
        num_encoder_layers = args.num_encoder_layers
        self.top_k = args.k_neighbors
        self.num_rbf = 16
        self.num_positional_embeddings = 16

        self.virtual_atoms = nn.Parameter(torch.rand(self.args.virtual_num,3))

        node_in = 0
        if self.args.node_dist:
            pair_num = 6
            if self.args.virtual_num>0:
                pair_num += self.args.virtual_num*(self.args.virtual_num-1)
            node_in += pair_num*self.num_rbf
        if self.args.node_angle:
            node_in += 12
        if self.args.node_direct:
            node_in += 9
        
        edge_in = 0
        if self.args.edge_dist:
            pair_num = 16
            
            if self.args.virtual_num>0:
                pair_num += self.args.virtual_num
                pair_num += self.args.virtual_num*(self.args.virtual_num-1)
            edge_in += pair_num*self.num_rbf
        if self.args.edge_angle:
            edge_in += 4
        if self.args.edge_direct:
            edge_in += 12

        self.node_embedding = nn.Linear(node_in, node_features, bias=True)
        self.edge_embedding = nn.Linear(edge_in, edge_features, bias=True)
        self.norm_nodes = nn.BatchNorm1d(node_features)
        self.norm_edges = nn.BatchNorm1d(edge_features)

        self.W_v = nn.Sequential(
            nn.Linear(node_features, hidden_dim, bias=True),
            nn.LeakyReLU(),
            nn.BatchNorm1d(hidden_dim),
            nn.Linear(hidden_dim, hidden_dim, bias=True),
            nn.LeakyReLU(),
            nn.BatchNorm1d(hidden_dim),
            nn.Linear(hidden_dim, hidden_dim, bias=True)
        )
        
        self.W_e = nn.Linear(edge_features, hidden_dim, bias=True) 
        self.W_f = nn.Linear(edge_features, hidden_dim, bias=True)

        self.encoder = StructureEncoder(hidden_dim, num_encoder_layers, dropout)

        self.decoder = MLPDecoder(hidden_dim)
        self._init_params()

        self.encode_t = 0
        self.decode_t = 0
    
    def forward(self, h_V, h_P, P_idx, batch_id, S=None, AT_test = False, mask_bw = None, mask_fw = None, decoding_order= None, return_logit=False):
        t1 = time.time()
        h_V = self.W_v(self.norm_nodes(self.node_embedding(h_V)))
        h_P = self.W_e(self.norm_edges(self.edge_embedding(h_P)))
        
        h_V, h_P = self.encoder(h_V, h_P, P_idx, batch_id)
        t2 = time.time()

        log_probs, logits = self.decoder(h_V, batch_id)
                
        t3 = time.time()
        

        self.encode_t += t2-t1
        self.decode_t += t3-t2

        if return_logit == True:
            return log_probs, logits
        return log_probs
        
    def _init_params(self):
        for name, p in self.named_parameters():
            if name == 'virtual_atoms':
                continue
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def _full_dist(self, X, mask, top_k=30, eps=1E-6):
        mask_2D = torch.unsqueeze(mask,1) * torch.unsqueeze(mask,2)
        dX = torch.unsqueeze(X,1) - torch.unsqueeze(X,2)
        D = (1. - mask_2D)*10000 + mask_2D* torch.sqrt(torch.sum(dX**2, 3) + eps)

        D_max, _ = torch.max(D, -1, keepdim=True)
        D_adjust = D + (1. - mask_2D) * (D_max+1)
        D_neighbors, E_idx = torch.topk(D_adjust, min(top_k, D_adjust.shape[-1]), dim=-1, largest=False)
        return D_neighbors, E_idx  

    def _get_features(self, score, X, mask):
        device = X.device
        mask_bool = (mask==1)
        B, N, _,_ = X.shape
        X_ca = X[:,:,1,:]
        D_neighbors, E_idx = self._full_dist(X_ca, mask, self.top_k)

        mask_attend = gather_nodes(mask.unsqueeze(-1), E_idx).squeeze(-1)
        mask_attend = (mask.unsqueeze(-1) * mask_attend) == 1
        edge_mask_select = lambda x:  torch.masked_select(x, mask_attend.unsqueeze(-1)).reshape(-1,x.shape[-1])
        node_mask_select = lambda x: torch.masked_select(x, mask_bool.unsqueeze(-1)).reshape(-1, x.shape[-1])

        randn = torch.rand(mask.shape, device=X.device)+5
        decoding_order = torch.argsort(-mask*(torch.abs(randn))) #我们的mask=1代表数据可用, 而protein MPP的mask=1代表数据不可用，正好相反
        mask_size = mask.shape[1]
        permutation_matrix_reverse = torch.nn.functional.one_hot(decoding_order, num_classes=mask_size).float()
        # 计算q已知的情况下, q->p的mask, 
        order_mask_backward = torch.einsum('ij, biq, bjp->bqp',(1-torch.triu(torch.ones(mask_size,mask_size, device=device))), permutation_matrix_reverse, permutation_matrix_reverse)
        mask_attend2 = torch.gather(order_mask_backward, 2, E_idx)
        mask_1D = mask.view([mask.size(0), mask.size(1), 1])
        mask_bw = (mask_1D * mask_attend2).unsqueeze(-1)
        mask_fw = (mask_1D * (1-mask_attend2)).unsqueeze(-1)
        mask_bw = edge_mask_select(mask_bw).squeeze()
        mask_fw = edge_mask_select(mask_fw).squeeze()
        

        # sequence
        if score is not None:
            score = torch.masked_select(score, mask_bool)

        # angle & direction
        V_angles = _dihedrals(X, 0) 
        V_angles = node_mask_select(V_angles)

        V_direct, E_direct, E_angles = _orientations_coarse_gl_tuple(X, E_idx)
        V_direct = node_mask_select(V_direct)
        E_direct = edge_mask_select(E_direct)
        E_angles = edge_mask_select(E_angles)

        # distance
        atom_N = X[:,:,0,:]
        atom_Ca = X[:,:,1,:]
        atom_C = X[:,:,2,:]
        atom_O = X[:,:,3,:]
        b = atom_Ca - atom_N
        c = atom_C - atom_Ca
        a = torch.cross(b, c, dim=-1)

        if self.args.virtual_num>0:
            virtual_atoms = self.virtual_atoms / torch.norm(self.virtual_atoms, dim=1, keepdim=True)
            for i in range(self.virtual_atoms.shape[0]):
                vars()['atom_v' + str(i)] = virtual_atoms[i][0] * a \
                                        + virtual_atoms[i][1] * b \
                                        + virtual_atoms[i][2] * c \
                                        + 1 * atom_Ca

        node_list = ['Ca-N', 'Ca-C', 'Ca-O', 'N-C', 'N-O', 'O-C']
        node_dist = []
        for pair in node_list:
            atom1, atom2 = pair.split('-')
            node_dist.append( node_mask_select(_get_rbf(vars()['atom_' + atom1], vars()['atom_' + atom2], None, self.num_rbf).squeeze()))
        
        if self.args.virtual_num>0:
            for i in range(self.virtual_atoms.shape[0]):
                # # true atoms
                for j in range(0, i):
                    node_dist.append(node_mask_select(_get_rbf(vars()['atom_v' + str(i)], vars()['atom_v' + str(j)], None, self.num_rbf).squeeze()))
                    node_dist.append(node_mask_select(_get_rbf(vars()['atom_v' + str(j)], vars()['atom_v' + str(i)], None, self.num_rbf).squeeze()))
        V_dist = torch.cat(tuple(node_dist), dim=-1).squeeze()
        

        pair_lst = ['Ca-Ca', 'Ca-C', 'C-Ca', 'Ca-N', 'N-Ca', 'Ca-O', 'O-Ca', 'C-C', 'C-N', 'N-C', 'C-O', 'O-C', 'N-N', 'N-O', 'O-N', 'O-O']

        edge_dist = [] #Ca-Ca
        for pair in pair_lst:
            atom1, atom2 = pair.split('-')
            rbf = _get_rbf(vars()['atom_' + atom1], vars()['atom_' + atom2], E_idx, self.num_rbf)
            edge_dist.append(edge_mask_select(rbf))

        if self.args.virtual_num>0:
            for i in range(self.virtual_atoms.shape[0]):
                edge_dist.append(edge_mask_select(_get_rbf(vars()['atom_v' + str(i)], vars()['atom_v' + str(i)], E_idx, self.num_rbf)))
                for j in range(0, i):
                    edge_dist.append(edge_mask_select(_get_rbf(vars()['atom_v' + str(i)], vars()['atom_v' + str(j)], E_idx, self.num_rbf)))
                    edge_dist.append(edge_mask_select(_get_rbf(vars()['atom_v' + str(j)], vars()['atom_v' + str(i)], E_idx, self.num_rbf)))

        
        E_dist = torch.cat(tuple(edge_dist), dim=-1)

        h_V = []
        if self.args.node_dist:
            h_V.append(V_dist)
        if self.args.node_angle:
            h_V.append(V_angles)
        if self.args.node_direct:
            h_V.append(V_direct)
        
        h_E = []
        if self.args.edge_dist:
            h_E.append(E_dist)
        if self.args.edge_angle:
            h_E.append(E_angles)
        if self.args.edge_direct:
            h_E.append(E_direct)
        
        _V = torch.cat(h_V, dim=-1)
        _E = torch.cat(h_E, dim=-1)
        
        # edge index
        shift = mask.sum(dim=1).cumsum(dim=0) - mask.sum(dim=1)
        src = shift.view(B,1,1) + E_idx
        src = torch.masked_select(src, mask_attend).view(1,-1)
        dst = shift.view(B,1,1) + torch.arange(0, N, device=src.device).view(1,-1,1).expand_as(mask_attend)
        dst = torch.masked_select(dst, mask_attend).view(1,-1)
        E_idx = torch.cat((dst, src), dim=0).long()

        decoding_order = node_mask_select((decoding_order+shift.view(-1,1)).unsqueeze(-1)).squeeze().long()
        
        # 3D point
        sparse_idx = mask.nonzero()  # index of non-zero values
        X = X[sparse_idx[:,0], sparse_idx[:,1], :, :]
        batch_id = sparse_idx[:,0]

        return X, score, _V, _E, E_idx, batch_id, mask_bw, mask_fw, decoding_order