# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import math
import warnings

import torch
from torch.distributions import constraints
from torch.distributions.utils import lazy_property

from .torch import Categorical
from .torch_distribution import TorchDistribution


class OneTwoMatchingConstraint(constraints.Constraint):
    def __init__(self, num_destins):
        self.num_destins = num_destins
        self.num_sources = 2 * num_destins

    def check(self, value):
        if value.dim() == 0:
            warnings.warn("Invalid event_shape: ()")
            return torch.tensor(False)
        batch_shape, event_shape = value.shape[:-1], value.shape[-1:]
        if event_shape != (self.num_sources,):
            warnings.warn("Invalid event_shape: {}".format(event_shape))
            return torch.tensor(False)
        if value.min() < 0 or value.max() >= self.num_destins:
            warnings.warn("Value out of bounds")
            return torch.tensor(False)
        counts = torch.zeros(batch_shape + (self.num_destins,))
        counts.scatter_add_(-1, value, torch.ones(value.shape))
        if (counts != 2).any():
            warnings.warn("Matching is not binary")
            return torch.tensor(False)
        return torch.tensor(True)


class OneTwoMatching(TorchDistribution):
    r"""
    Random matching from ``2*N`` sources to ``N`` destinations where each
    source matches exactly **one** destination and each destination matches
    exactly **two** sources. Samples are represented as long tensors of shape
    ``(2*N,)`` taking values in ``{0,...,N-1}`` and satisfying the above
    one-two constraint. The log probability of a sample ``v`` is the sum of
    edge logits, up to the log partition function ``log Z``:

    .. math::

        \log p(v) = \sum_s \text{logits}[s, v[s]] - \log Z

    Exact computations are expensive. To enable tractable approximations, set a
    number of belief propagation iterations via the ``bp_iters`` argument.  The
    :meth:`log_partition_function` and :meth:`log_prob` methods use a Bethe
    approximation [1,2,3] with fractional free energy [4].

    **References:**

    [1] Michael Chertkov, Lukas Kroc, Massimo Vergassola (2008)
        "Belief propagation and beyond for particle tracking"
        https://arxiv.org/pdf/0806.1199.pdf
    [2] Bert Huang, Tony Jebara (2009)
        "Approximating the Permanent with Belief Propagation"
        https://arxiv.org/pdf/0908.1769.pdf
    [3] Pascal O. Vontobel (2012)
        "The Bethe Permanent of a Non-Negative Matrix"
        https://arxiv.org/pdf/1107.4196.pdf
    [4] M Chertkov, AB Yedidia (2013)
        "Approximating the permanent with fractional belief propagation"
        http://www.jmlr.org/papers/volume14/chertkov13a/chertkov13a.pdf

    :param Tensor logits: An ``(2 * N, N)``-shaped tensor of edge logits.
    :param int bp_iters: Optional number of belief propagation iterations. If
        unspecified or ``None`` expensive exact algorithms will be used.
    """
    arg_constraints = {"logits": constraints.real}
    has_enumerate_support = True

    def __init__(self, logits, *, bp_iters=None, validate_args=None):
        if logits.dim() != 2:
            raise NotImplementedError("OneTwoMatching does not support batching")
        assert bp_iters is None or isinstance(bp_iters, int) and bp_iters > 0
        self.num_sources, self.num_destins = logits.shape
        assert self.num_sources == 2 * self.num_destins
        self.logits = logits
        batch_shape = ()
        event_shape = (self.num_sources,)
        super().__init__(batch_shape, event_shape, validate_args=validate_args)
        self.bp_iters = bp_iters

    @constraints.dependent_property
    def support(self):
        return OneTwoMatchingConstraint(self.num_destins)

    @lazy_property
    def log_partition_function(self):
        if self.bp_iters is None:
            # Brute force.
            d = self.enumerate_support()
            s = torch.arange(d.size(-1), dtype=d.dtype, device=d.device)
            return self.logits[s, d].sum(-1).logsumexp(-1)

        # Approximate mean field beliefs b via Sinkhorn iteration.
        # We find that Sinkhorn iteration is more robust and faster than the
        # optimal belief propagation updates suggested in [1-4].
        finfo = torch.finfo(self.logits.dtype)
        shift = self.logits.data.max(1, True).values
        shift.clamp_(min=finfo.min, max=finfo.max)
        p = (self.logits - shift).exp().clamp(min=finfo.tiny ** 0.5)
        d = 2 / p.sum(0)
        for _ in range(self.bp_iters):
            s = 1 / (p @ d)
            d = 2 / (s @ p)
        b = s[:, None] * d * p

        # Evaluate the Bethe free energy, adapting [4] Eqn 4 to one-two
        # matchings.
        b = b.clamp(min=finfo.tiny ** 0.5)
        b_ = (1 - b).clamp(min=finfo.tiny ** 0.5)
        internal_energy = -(b * p.log()).sum()
        # Let h be the entropy of matching each destin to one source.
        z = b / 2
        h = -(z * z.log()).sum(0)
        # Then h2 is the entropy of the distribution mapping each destin to an
        # unordered pair of sources, equal to the log of the binomial
        # coefficient: perplexity * (perplexity - 1) / 2.
        h2 = h + h.expm1().log() - math.log(2)
        free_energy = internal_energy - h2.sum() - (b_ * b_.log()).sum()
        return shift.sum() - free_energy

    def log_prob(self, value):
        if self._validate_args:
            self._validate_sample(value)
        d = value
        s = torch.arange(d.size(-1), dtype=d.dtype, device=d.device)
        return self.logits[s, d].sum(-1) - self.log_partition_function

    def enumerate_support(self, expand=True):
        return enumerate_one_two_matchings(self.num_destins)

    def sample(self, sample_shape=torch.Size()):
        if self.bp_iters is None:
            # Brute force.
            d = self.enumerate_support()
            s = torch.arange(d.size(-1), dtype=d.dtype, device=d.device)
            logits = self.logits[s, d].sum(-1)
            sample = Categorical(logits=logits).sample(sample_shape)
            return d[sample]

        if sample_shape:
            return torch.stack([self.sample(sample_shape[1:])
                                for _ in range(sample_shape[0])])
        # Consider initializing with heuristic or MAP
        # https://arxiv.org/pdf/0709.1190
        # http://proceedings.mlr.press/v15/huang11a/huang11a.pdf
        # followed by a small number of MCMC steps
        # http://www.cs.toronto.edu/~mvolkovs/nips2012_sampling.pdf
        raise NotImplementedError

    def mode(self):
        return min_cost_one_two_matching(self.logits)


def enumerate_one_two_matchings(num_destins):
    if num_destins == 1:
        return torch.tensor([[0, 0]])

    num_sources = num_destins * 2
    subproblem = enumerate_one_two_matchings(num_destins - 1)
    subsize = subproblem.size(0)
    result = torch.empty(subsize * num_sources * (num_sources - 1) // 2,
                         num_sources,
                         dtype=torch.long)

    # Iterate over pairs of sources s0<s1 matching the last destination d.
    d = num_destins - 1
    pos = 0
    for s1 in range(num_sources):
        for s0 in range(s1):
            block = result[pos:pos+subsize]
            block[:, :s0] = subproblem[:, :s0]
            block[:, s0] = d
            block[:, s0 + 1:s1] = subproblem[:, s0:s1 - 1]
            block[:, s1] = d
            block[:, s1 + 1:] = subproblem[:, s1 - 1:]
            pos += subsize
    return result


@torch.no_grad()
def min_cost_one_two_matching(logits, max_iters=100):
    num_sources, num_destins = logits.shape
    assert num_sources == 2 * num_destins
    if num_destins == 1:
        return logits.new_zeros(num_sources, dtype=torch.long)
    s = torch.arange(num_sources)
    d = torch.arange(num_destins)

    # Add jitter to ensure entries are unique, as required for convergence.
    finfo = torch.finfo(logits.dtype)
    logits = logits.clamp(min=finfo.min * finfo.eps, max=finfo.max * finfo.eps)
    jitter = torch.empty_like(logits).uniform_() - 0.5
    logits *= 1 + finfo.eps ** 0.5 * jitter

    # Perform loopy max-sum belief propagation.
    m_sd = torch.zeros_like(logits)
    m_ds = torch.zeros_like(logits)
    conflicts = torch.ones_like(logits, dtype=torch.bool)
    for step in range(max_iters):
        # For each source choose the best destin.
        objective = logits + m_ds
        values, indices = objective.T.topk(2, dim=0)
        m_sd[:] = objective - values[0, :, None]
        m_sd[s, indices[0]] = values[0] - values[1]
        m_sd -= m_ds
        result = indices[0]

        # For each destin choose the best two sources.
        objective = m_sd
        values, indices = objective.topk(3, dim=0)
        m_ds[:] = objective - values[1]
        m_ds[indices[0], d] = values[0] - values[2]
        m_ds[indices[1], d] = values[1] - values[2]
        m_ds -= m_sd

        # Return when the two local optimizers agree.
        conflicts[:] = False
        conflicts[s, result] = True
        conflicts[indices[0], d] = False
        conflicts[indices[1], d] = False
        if not conflicts.any():
            return result

    raise ValueError("Failed to converge after {} iterations; "
                     "try adding jitter so that logit values are unique."
                     .format(max_iters))