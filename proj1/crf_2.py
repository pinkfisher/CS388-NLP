import torch
from torch import nn
import numpy as np


class CRF(nn.Module):

    def __init__(
        self, num_features, nb_labels, pad_tag_id=None, batch_first=True):
        super().__init__()

        print("CRF model instantiated with num_features = {} and nb_labels = {}".format(num_features, nb_labels))

        self.nb_labels = nb_labels + 2
        self.num_features = num_features
        self.BOS_TAG_ID = nb_labels
        self.EOS_TAG_ID = nb_labels + 1

        self.transitions = torch.ones(self.nb_labels, self.nb_labels, requires_grad=False)

        #I-PER
        self.transitions.data[:, 4] = -100.0
        self.transitions.data[3, 4] = 1.0

        #I-ORG
        self.transitions.data[:, 6] = -100.0
        self.transitions.data[1, 6] = 1.0

        #I_MISC
        self.transitions.data[:, 7] = -100.0
        self.transitions.data[2, 7] = 1.0

        #I_LOC
        self.transitions.data[:, 8] = -100.0
        self.transitions.data[5, 8] = 1.0

        self.transitions.data[:, self.BOS_TAG_ID] = -10000.0
        self.transitions.data[self.EOS_TAG_ID, :] = -10000.0


        self.emmision_weights = nn.Parameter(torch.empty(num_features, 1))
        nn.init.uniform_(self.emmision_weights, -0.1, 0.1)

        self.num_features = num_features

    def get_emssions(self, seq_x):
        '''
        input of dims (seq_length, nb_labels, 14 indexes)
        output must be dimensions (batch_size, seq_len, nb_labels)
        '''
        seq_length = seq_x.shape[0]
        nb_labels = seq_x.shape[1]
        num_active_indexes  = seq_x.shape[2]

        emmisions = []

        for i, x in enumerate(seq_x):
            ind2 = x.flatten()
            ind1 = np.array([i//14 for i in range(len(ind2))])

            indices = torch.from_numpy(np.stack((ind1, ind2)))
            values = torch.ones(len(ind2))

            features = torch.sparse.FloatTensor(indices = indices, values=values,\
            size=torch.Size([self.nb_labels, self.num_features]))
            potential = torch.sparse.mm(features, self.emmision_weights)
            emmisions.append(potential.squeeze())
            
        emmisions = torch.stack(emmisions).unsqueeze(0)
        return emmisions

    def viterbi_decode(self, emissions):

        batch_size, seq_length, nb_labels = emissions.shape
        alphas = self.transitions[self.BOS_TAG_ID, :].unsqueeze(0) + emissions[:, 0]
        backpointers = []

        for i in range(1, seq_length):
            alpha_t = []
            backpointers_t = []

            for tag in range(nb_labels):

                e_scores = emissions[:, i, tag]
                e_scores = e_scores.unsqueeze(1)
                t_scores = self.transitions[:, tag]
                t_scores = t_scores.unsqueeze(0)
                # print(e_scores)
                # print(t_scores)
                # exit(0)
                scores = e_scores + t_scores + alphas
                max_score, max_score_tag = torch.max(scores, dim=-1)
                alpha_t.append(max_score)
                backpointers_t.append(max_score_tag)

            new_alphas = torch.stack(alpha_t).t()
            alphas = new_alphas
            backpointers.append(backpointers_t)

        last_transition = self.transitions[:, self.EOS_TAG_ID]
        end_scores = alphas + last_transition.unsqueeze(0)
        max_final_scores, max_final_tags = torch.max(end_scores, dim=1)

        # find the best sequence of labels for each sample in the batch
        best_sequences = []
        all_emm = torch.ones(emissions.shape[:2])
        emission_lengths = all_emm.int().sum(dim=1)
        for i in range(batch_size):
            sample_length = emission_lengths[i].item()
            sample_final_tag = max_final_tags[i].item()
            sample_backpointers = backpointers[: sample_length - 1]

            # follow the backpointers to build the sequence of labels
            sample_path = self.find_best_path(i, sample_final_tag, sample_backpointers)
            # add this path to the list of best sequences
            best_sequences.append(sample_path)

        return max_final_scores, best_sequences

    def find_best_path(self, sample_id, best_tag, backpointers):

        best_path = [best_tag]
        for backpointers_t in reversed(backpointers):
            best_tag = backpointers_t[best_tag][sample_id].item()
            best_path.insert(0, best_tag)
        return best_path
    
    def forward(self, x):
        emissions = self.get_emssions(x)
        score, path = self.viterbi_decode(emissions)
        return path
    
    def loss(self, x, tags):
        """Compute the negative log-likelihood. See `log_likelihood` method."""
        emissions = self.get_emssions(x)
        tags = torch.from_numpy(tags)
        nll = -self.log_likelihood(emissions, tags)
        return nll

    def log_likelihood(self, emissions, tags):
        scores = self.compute_scores(emissions, tags)
        partition, end_scores = self.compute_log_partition(emissions)
        # print(torch.log(end_scores-scores))
        return torch.sum(scores - partition)

    def compute_scores(self, emissions, tags):

        batch_size, seq_length = tags.shape
        scores = torch.zeros(batch_size)
        first_tags = tags[:, 0]
        all_emm = torch.ones(emissions.shape[:2])
        last_valid_idx = all_emm.int().sum(1) - 1
        last_tags = tags.gather(1, last_valid_idx.unsqueeze(1)).squeeze()
        e_scores = emissions[:, 0].gather(1, first_tags.unsqueeze(1)).squeeze()
        scores += e_scores

        for i in range(1, seq_length):
            previous_tags = tags[:, i - 1]
            current_tags = tags[:, i]
            e_scores = emissions[:, i].gather(1, current_tags.unsqueeze(1)).squeeze()
            scores += e_scores

        return scores

    def compute_log_partition(self, emissions):

        batch_size, seq_length, nb_labels = emissions.shape
        alphas = self.transitions[self.BOS_TAG_ID, :].unsqueeze(0) + emissions[:, 0]

        for i in range(1, seq_length):
            alpha_t = []

            for tag in range(nb_labels):

                e_scores = emissions[:, i, tag]
                e_scores = e_scores.unsqueeze(1)

                # combine current scores with previous alphas
                scores = e_scores + alphas

                # add the new alphas for the current tag
                alpha_t.append(torch.logsumexp(scores, dim=1))

            new_alphas = torch.stack(alpha_t).t()
            alphas = new_alphas

        # add the scores for the final transition
        end_scores = alphas

        # return a *log* of sums of exps
        return torch.logsumexp(end_scores, dim=1), end_scores

