# Copyright 2022 - Valeo Comfort and Driving Assistance - Gilles Puy @ valeo.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import torch.nn as nn
from .backbone import WaffleIron
from .embedding import Embedding


class Segmenter(nn.Module):
    def __init__(
        self,
        input_channels,
        feat_channels,
        nb_class,
        depth,
        grid_shape,
        drop_path_prob=0,
        layer_norm=False,
        early_exit=None,
    ):
        super().__init__()
        # Embedding layer
        self.embed = Embedding(input_channels, feat_channels)
        # WaffleIron backbone
        self.waffleiron = WaffleIron(feat_channels, depth, grid_shape, drop_path_prob, layer_norm, early_exit)
        # Classification layer
        self.classif = nn.Conv1d(feat_channels, nb_class, 1)

    def compress(self):
        self.embed.compress()
        self.waffleiron.compress()

    def forward(self, feats, cell_ind, occupied_cell, neighbors):

        tokens_1 = self.embed(feats, neighbors) # radius can change based on the local density 
        # Node classification -> self and its neighbors
        tokens, exit_layer = self.waffleiron(tokens_1, cell_ind, occupied_cell)

        #if all_features:
        #    return tokens_1, tokens, self.classif(tokens[-1])
        #else:
        return tokens_1, tokens, self.classif(tokens), exit_layer