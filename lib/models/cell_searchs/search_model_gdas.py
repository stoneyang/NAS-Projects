##################################################
# Copyright (c) Xuanyi Dong [GitHub D-X-Y], 2019 #
###########################################################################
# Searching for A Robust Neural Architecture in Four GPU Hours, CVPR 2019 #
###########################################################################
import torch
import torch.nn as nn
from copy import deepcopy
from .infer_cells  import ResNetBasicblock
from .search_cells import SearchCell
from .genotypes    import Structure


class TinyNetworkGDAS(nn.Module):

  def __init__(self, C, N, max_nodes, num_classes, search_space):
    super(TinyNetworkGDAS, self).__init__()
    self._C        = C
    self._layerN   = N
    self.max_nodes = max_nodes
    self.stem = nn.Sequential(
                    nn.Conv2d(3, C, kernel_size=3, padding=1, bias=False),
                    nn.BatchNorm2d(C))
  
    layer_channels   = [C    ] * N + [C*2 ] + [C*2  ] * N + [C*4 ] + [C*4  ] * N    
    layer_reductions = [False] * N + [True] + [False] * N + [True] + [False] * N

    C_prev, num_edge, edge2index = C, None, None
    self.cells = nn.ModuleList()
    for index, (C_curr, reduction) in enumerate(zip(layer_channels, layer_reductions)):
      if reduction:
        cell = ResNetBasicblock(C_prev, C_curr, 2)
      else:
        cell = SearchCell(C_prev, C_curr, 1, max_nodes, search_space)
        if num_edge is None: num_edge, edge2index = cell.num_edges, cell.edge2index
        else: assert num_edge == cell.num_edges and edge2index == cell.edge2index, 'invalid {:} vs. {:}.'.format(num_edge, cell.num_edges)
      self.cells.append( cell )
      C_prev = cell.out_dim
    self.op_names   = deepcopy( search_space )
    self._Layer     = len(self.cells)
    self.edge2index = edge2index
    self.lastact    = nn.Sequential(nn.BatchNorm2d(C_prev), nn.ReLU(inplace=True))
    self.global_pooling = nn.AdaptiveAvgPool2d(1)
    self.classifier = nn.Linear(C_prev, num_classes)
    self.arch_parameters = nn.Parameter( 1e-3*torch.randn(num_edge, len(search_space)) )
    self.tau        = 10
    self.nan_count  = 0

  def get_weights(self):
    xlist = list( self.stem.parameters() ) + list( self.cells.parameters() )
    xlist+= list( self.lastact.parameters() ) + list( self.global_pooling.parameters() )
    xlist+= list( self.classifier.parameters() )
    return xlist

  def set_tau(self, tau, _nan_count=0):
    self.tau = tau
    self.nan_count = _nan_count

  def get_tau(self):
    return self.tau

  def get_alphas(self):
    return [self.arch_parameters]

  def get_message(self):
    string = self.extra_repr()
    for i, cell in enumerate(self.cells):
      string += '\n {:02d}/{:02d} :: {:}'.format(i, len(self.cells), cell.extra_repr())
    return string

  def extra_repr(self):
    return ('{name}(C={_C}, Max-Nodes={max_nodes}, N={_layerN}, L={_Layer})'.format(name=self.__class__.__name__, **self.__dict__))

  def genotype(self):
    genotypes = []
    for i in range(1, self.max_nodes):
      xlist = []
      for j in range(i):
        node_str = '{:}<-{:}'.format(i, j)
        with torch.no_grad():
          weights = self.arch_parameters[ self.edge2index[node_str] ]
          op_name = self.op_names[ weights.argmax().item() ]
        xlist.append((op_name, j))
      genotypes.append( tuple(xlist) )
    return Structure( genotypes )

  def forward(self, inputs):
    def gumbel_softmax(_logits, _tau):
      while True: # a trick to avoid the gumbels bug
        gumbels    = -torch.empty_like(_logits).exponential_().log()
        new_logits = (_logits.log_softmax(dim=1) + gumbels) / _tau
        probs      = nn.functional.softmax(new_logits, dim=1)
        index      = probs.max(-1, keepdim=True)[1]
        if index[0].item() == self.op_names.index('none') and index[3].item() == self.op_names.index('none') and index[5].item() == self.op_names.index('none'): continue
        if index[1].item() == self.op_names.index('none') and index[2].item() == self.op_names.index('none') and index[3].item() == self.op_names.index('none') and index[4].item() == self.op_names.index('none'): continue
        if index[3].item() == self.op_names.index('none') and index[4].item() == self.op_names.index('none') and index[5].item() == self.op_names.index('none'): continue
        if index[3].item() == self.op_names.index('none') and index[0].item() == self.op_names.index('none') and index[1].item() == self.op_names.index('none'): continue
        one_h      = torch.zeros_like(_logits).scatter_(-1, index, 1.0)
        xres       = one_h - probs.detach() + probs
        if (not torch.isinf(gumbels).any()) and (not torch.isinf(probs).any()) and (not torch.isnan(probs).any()): break
        self.nan_count += 1
      return xres, index

    feature = self.stem(inputs)
    for i, cell in enumerate(self.cells):
      if isinstance(cell, SearchCell):
        alphas, IDX  = gumbel_softmax(self.arch_parameters, self.tau)
        feature = cell.forward_gdas(feature, alphas, IDX.cpu())
      else:
        feature = cell(feature)

    out = self.lastact(feature)
    out = self.global_pooling( out )
    out = out.view(out.size(0), -1)
    logits = self.classifier(out)

    return out, logits
