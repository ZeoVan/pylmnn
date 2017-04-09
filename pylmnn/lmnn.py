# coding: utf-8
"""
Large Margin Nearest Neighbor Classification
"""

# Author: John Chiotellis <johnyc.code@gmail.com>
#
# License: BSD 3 clause

from __future__ import print_function
from sklearn import warnings

import numpy as np
import os
import time
from scipy.optimize import fmin_l_bfgs_b
from scipy.sparse import csr_matrix, spdiags

from sklearn.neighbors import KNeighborsClassifier
from sklearn.metrics.pairwise import euclidean_distances
from sklearn.utils import gen_batches
from sklearn.utils.extmath import row_norms
from sklearn.utils.fixes import argpartition, partition, sp_version, bincount
from sklearn.utils.random import choice, check_random_state
from sklearn.utils.multiclass import check_classification_targets
from sklearn.utils.validation import check_is_fitted, check_array, check_X_y
from sklearn.exceptions import DataDimensionalityWarning


class LargeMarginNearestNeighbor(KNeighborsClassifier):
    """Distance Metric Learning for Large Margin Classification

    Large margin nearest neighbor classification (LMNN) is a machine learning
    algorithm for metric learning. It learns a (pseudo-)metric in a
    supervised fashion to improve the classification accuracy of the k-nearest
    neighbor rule.
    The main intuition behind LMNN is to learn a pseudometric under which all
    data instances in the training set are surrounded by at least k instances
    that share the same class label. If this is achieved, the leave-one-out
    error is minimized.
    This implementation follows closely Kilian Weinberger's MATLAB code found
    at <https://bitbucket.org/mlcircus/lmnn> which solves the unconstrained
    problem, finding a linear transformation with L-BFGS instead of solving the
    constrained problem that finds the globally optimal metric.


    Parameters
    ----------
    L : array, shape (n_features_out, n_features_in), optional (default=None)
        An initial linear transformation. If None (default), the initial
        transformation is set to the identity, except if ``warm_start`` or
        ``use_pca`` is True.

    warm_start : bool, optional (default=False)
        If True and :meth:`fit` has been called before, use the solution of the
        previous call to :meth:`fit` to initialize the linear transformation.

    use_pca : bool, optional (default=True)
        Whether to use PCA to initialize the linear transformation.
        If False, the identity will be used, except if ``warm_start`` is True.

    n_features_out : int, optional (default=None)
        Preferred dimensionality of the inputs after the transformation.
        If None it is inferred from ``use_pca`` and ``L``.

    n_neighbors : int, optional (default=3)
        Number of target neighbors.

    max_iter : int, optional (default=200)
        Maximum number of iterations in the optimization.

    tol : float, optional (default=1e-5)
        Convergence tolerance for the optimization.

    max_constraints : int, optional (default=500000)
        Maximum number of constraints to enforce per iteration.

    use_sparse : bool, optional (default=True)
        Whether to use a sparse matrix (default) or a dense matrix for the
        impostor-pairs storage. Using a sparse matrix, the distance to
        impostors is computed twice, but it is somewhat faster for larger
        data sets than using a dense matrix. With a dense matrix, the unique
        impostor pairs have to be identified explicitly.

    max_corrections : int, optional (default=100)
        The maximum number of variable metric corrections
        used to define the limited memory matrix. (The limited memory BFGS
        method does not store the full hessian but uses this many terms in an
        approximation to it.)

    verbose : int, optional (default=0)
        If 0, no progress messages will be printed.
        If 1, progress messages will be printed to stdout.
        If >1, progress messages will be printed and the ``iprint``
        parameter of :meth:`fmin_l_bfgs_b` of `scipy.optimize` will be set to
        verbose - 2.

    random_state : int or numpy.RandomState or None, optional (default=None)
        A pseudo random number generator used for sampling the constraints.

    n_jobs : int, optional (default=1)
        The number of parallel jobs to run for neighbors search.
        If ``-1``, then the number of jobs is set to the number of CPU cores.
        Doesn't affect :meth:`fit` method.

    Attributes
    ----------
    L_ : array, shape (n_features_out, n_features_in).
        The linear transformation used during fitting.

    n_neighbors_ : int
        The provided n_neighbors is decreased when >= min(number of
        elements in each class).

    n_features_out_ : int
        The dimensionality of a sample's vector after applying to it the
        linear transformation.

    classes_ : array-like, shape (n_classes,)
        The appearing class labels.

    n_funcalls_ : int
        The number of times the optimizer computes the loss and the gradient.

    n_iter_ : int
        The number of iterations of the optimizer. Falls back to
        `n_funcalls` if the version of :meth:`fmin_l_bfgs_b` of
        `scipy.optimize` (< 0.12.0) does not store the number of iterations.

    details_ : dict
        A dictionary of information created by the L-BFGS optimizer during
        fitting.


    Examples
    --------
    >>> X = [[0], [1], [2], [3]]
    >>> y = [0, 0, 1, 1]
    >>> from pylmnn.lmnn import LargeMarginNearestNeighbor
    >>> lmnn = LargeMarginNearestNeighbor(n_neighbors=1)
    >>> lmnn.fit(X, y) # doctest: +ELLIPSIS
    LargeMarginNearestNeighbor(...)
    >>> print(lmnn.predict([[1.1]]))
    [0]
    >>> print(lmnn.predict_proba([[0.9]]))
    [[ 1.  0.]]


    References
    ----------
    .. [1] Weinberger, Kilian Q., and Lawrence K. Saul. "Distance Metric
    Learning for Large Margin Nearest Neighbor Classification."
    Journal of Machine Learning Research, Vol. 10, Feb. 2009, pp. 207-244.
    (http://jmlr.csail.mit.edu/papers/volume10/weinberger09a/weinberger09a.pdf)

    .. [2] Wikipedia entry on Large Margin Nearest Neighbor
    (https://en.wikipedia.org/wiki/Large_margin_nearest_neighbor)

    """

    def __init__(self, L=None, warm_start=False, use_pca=True,
                 n_features_out=None, n_neighbors=3, max_iter=200, tol=1e-5,
                 max_constraints=500000, use_sparse=True,
                 max_corrections=100, verbose=0, random_state=None, n_jobs=1):

        super(LargeMarginNearestNeighbor, self).__init__(
            n_neighbors=n_neighbors, n_jobs=n_jobs)

        # Parameters
        self.L = L
        self.n_features_out = n_features_out
        self.max_iter = max_iter
        self.tol = tol
        self.use_pca = use_pca
        self.max_constraints = max_constraints
        self.use_sparse = use_sparse
        self.warm_start = warm_start
        self.max_corrections = max_corrections
        self.verbose = verbose
        self.random_state = random_state
        self.n_jobs = n_jobs

    def fit(self, X, y):
        """Find a linear transformation by optimization of the unconstrained
        problem, such that the k-nearest neighbor classification accuracy
        improves.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features_in)
            The training samples.

        y : array-like, shape (n_samples,)
            The corresponding training labels.

        Returns
        -------
        self : returns a trained LargeMarginNearestNeighbor model.
        """

        # Check inputs consistency
        X, y_ = self._validate_params(X, y)

        self.random_state_ = check_random_state(self.random_state)

        # Initialize transformer
        L, self.n_features_out_ = self._init_transformer(X)

        # Find the target neighbors
        targets = self._select_target_neighbors(X, y_)

        # Compute gradient component of target neighbors
        grad_static = self._compute_grad_static(X, targets)

        # Initialize number of optimizer iterations and objective funcalls
        self.n_iter_ = 0
        self.n_funcalls_ = 0
        iprint = self.verbose - 2 if self.verbose > 1 else -1

        # For older versions of fmin, x0 needs to be a vector
        L = L.ravel()

        # Create parameter dict for optimizer
        optimizer_dict = {'func': self._loss_grad, 'x0': L, 'iprint': iprint,
                          'm': self.max_corrections, 'pgtol': self.tol,
                          'args': (X, y_, targets, grad_static)}

        if sp_version >= (0, 12, 0):
            optimizer_dict['maxiter'] = self.max_iter
            if self.verbose:
                optimizer_dict['callback'] = self._lbfgs_callback
        else:  # Type Error caused in old versions of SciPy (<= 0.11.0)
            # because of no maxiter argument.
            optimizer_dict['maxfun'] = self.max_iter

        if self.verbose:
            print('{:^9}\t{:^13}\t{:^20}\t{:^8}'.
                  format('Iteration', 'Function Call', 'Loss', 'Time (s)'))
            print('-'*60)
            print('{:^9}'.format(self.n_iter_))

        # Call optimizer
        L, loss, info = fmin_l_bfgs_b(**optimizer_dict)

        # Reshape result from optimizer
        self.L_ = L.reshape(self.n_features_out_, L.size //
                            self.n_features_out_)

        # Get number of iterations or function calls from the optimizer
        self.n_iter_ = info.get('nit', info['funcalls'])

        # Store output to return
        self.details_ = info
        self.details_['loss'] = loss

        # Fit a simple nearest neighbor classifier with the learned metric
        super(LargeMarginNearestNeighbor, self).\
            fit(self.transform(X, check=False), y)

        return self

    def transform(self, X, check=True):
        """Applies the learned transformation to the inputs.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features_in)
            Data samples.

        check: bool, optional (default=True)
            Whether to validate ``X``.

        Returns
        -------
        Lx: array-like, shape (n_samples, n_features_out)
            The data samples transformed.
        """

        if check:
            X = check_array(X)

        return X.dot(self.L_.T)

    def predict(self, X):
        """Predict the class labels for the provided data

        Parameters
        ----------
        X : array-like, shape (n_query, n_features)
            Test samples.

        Returns
        -------
        y_pred : array, shape (n_query,)
            A predicted class label for each test sample.

        Raises
        ------
        NotFittedError
            If :meth:`fit` has not been called before.
        """

        # Check if fit has been called
        check_is_fitted(self, ['L_'])
        y_pred = super(LargeMarginNearestNeighbor, self).predict(
            self.transform(X))

        return y_pred

    def predict_proba(self, X):
        """Return probability estimates for the test data X.

        Parameters
        ----------
        X : array-like, shape (n_query, n_features)
            Test samples.

        Returns
        -------
        p : array of shape = [n_samples, n_classes], or a list of n_outputs
            of such arrays if n_outputs > 1.
            The class probabilities of the input samples. Classes are ordered
            by lexicographic order.

        Raises
        ------
        NotFittedError
            If :meth:`fit` has not been called before.
        """

        # Check if fit has been called
        check_is_fitted(self, ['L_'])
        probabilities = super(LargeMarginNearestNeighbor, self).predict_proba(
            self.transform(X))

        return probabilities

    def predict_energy(self, X_test):
        """Predict the class labels for the provided data based on energy
        minimization

        Parameters
        ----------
        X_test : array-like, shape (n_query, n_features)
            Test samples.

        Returns
        -------
        y_pred : array, shape (n_query,)
            A predicted class label for each test sample.

        Raises
        ------
        NotFittedError
            If :meth:`fit` has not been called before.
        """

        # Check if fit has been called
        check_is_fitted(self, ['L_'])

        Lx_train, y_train = self._fit_X, self._y
        X_train = Lx_train.dot(np.linalg.pinv(self.L_.T))

        y_pred = self._energy_classify_batch(X_train, y_train, X_test)

        return y_pred

    def _validate_params(self, X, y):
        """Validate input parameters as soon as :meth:`fit` is called.

        Parameters
        ----------
        X : array-like, shape (n_samples, n_features_in)
            The training samples.

        y : array-like, shape (n_samples,)
            The corresponding training labels.

        Returns
        -------
        X : array, shape (n_samples, n_features_in)
            The training samples.

        y_inversed : array, shape (n_samples,)
            The corresponding training labels.

        Raises
        -------
        TypeError
            If a parameter's type does not match the desired type.

        ValueError
            If a parameter's value violates its legal value range or if the
            combination of two or more given parameters is incompatible.
        """

        # Check training data
        X, y = check_X_y(X, y, ensure_min_samples=2)
        check_classification_targets(y)

        # Store the appearing classes and the class index for each sample
        classes, y_inverse = np.unique(y, return_inverse=True)

        # Check number of classes > 1
        n_classes = len(classes)
        if n_classes < 2:
            raise ValueError("LargeMarginNearestNeighbor requires 2 or more "
                             "distinct classes, got {}.".format(n_classes))

        # Check every class has at least 2 samples
        min_class_size = bincount(y_inverse).min()
        if min_class_size < 2:
            raise ValueError('At least one class has less than 2 ({}) '
                             'training samples.'.format(min_class_size))

        check_scalar(self.warm_start, 'warm_start', bool)
        if self.warm_start and hasattr(self, 'L_'):
            if set(classes) != set(self.classes_):
                raise ValueError("warm_start can only be used where `y` has "
                                 "the same classes as in the previous call to "
                                 "fit. Previously got {}, `y` has {}".
                                 format(self.classes_, classes))

            if len(self.L_[0]) != len(X[0]):
                raise ValueError('The new inputs dimensionality ({}) does not '
                                 'match the previously learned transformation '
                                 'input dimensionality ({}).'
                                 .format(len(self.L_[0]), len(X[0])))
        self.classes_ = classes

        if self.n_features_out is not None:
            check_scalar(self.n_features_out, 'n_features_out', int, 1)
        check_scalar(self.n_neighbors, 'n_neighbors', int, 1, len(X) - 1)
        check_scalar(self.max_iter, 'max_iter', int, 1)
        check_scalar(self.max_constraints, 'max_constraints', int, 1)
        check_scalar(self.max_corrections, 'max_corrections', int, 1)
        check_scalar(self.n_jobs, 'n_jobs', int, -1)

        check_scalar(self.tol, 'tol', float, 0.)

        check_scalar(self.use_pca, 'use_pca', bool)
        check_scalar(self.use_sparse, 'use_sparse', bool)
        check_scalar(self.verbose, 'verbose', int, 0)

        # Check linear transformation dimensions
        if self.L is not None:
            check_array(self.L)
            if len(self.L[0]) != len(X[0]):
                raise ValueError('Transformation input dimensionality ({}) '
                                 'must match the inputs dimensionality ({}).'
                                 .format(len(self.L[0]), len(X[0])))

            if len(self.L) > len(self.L[0]):
                raise ValueError('Transformation output dimensionality ({}) '
                                 'cannot be greater than the '
                                 'transformation input dimensionality ({}).'.
                                 format(len(self.L), len(self.L[0])))

        # Check preferred output dimensionality
        if self.n_features_out is not None:
            if self.L is not None:
                if self.n_features_out != len(self.L):
                    raise ValueError('Preferred outputs dimensionality ({}) '
                                     'does not match the given linear '
                                     'transformation {}!'.format(
                                        self.n_features_out, len(self.L)))

            elif self.n_features_out > len(X[0]):
                raise ValueError('Preferred outputs dimensionality ({}) '
                                 'cannot be greater than the given data '
                                 'dimensionality {}!'.format(
                                    self.n_features_out, len(X[0])))

        # Check preferred number of neighbors
        max_neighbors = min_class_size - 1
        if self.n_neighbors > max_neighbors:
            warnings.warn('n_neighbors(={}) too high. Setting to {}.'.
                          format(self.n_neighbors, max_neighbors))
        self.n_neighbors_ = min(self.n_neighbors, max_neighbors)
        # TODO: Notify superclass KNeighborsClassifier that n_neighbors
        # might have changed to n_neighbors_
        # super(LargeMarginNearestNeighbor, self).set_params(
        # n_neighbors=self.n_neighbors_)

        return X, y_inverse

    def _init_transformer(self, X):
        """Initialize the linear transformation by setting to user specified
        parameter, loading from a file, applying PCA or setting to identity.

        Parameters
        ----------
        X : array, shape (n_samples, n_features_in)
            Data samples.

        Returns
        -------
        L : array, shape (n_features_out, n_features_in)
            The initial linear transformation.
        """

        if self.L is not None:
            L = np.asarray(self.L)
        elif self.warm_start and hasattr(self, 'L_'):
            L = self.L_
        elif self.use_pca and X.shape[1] > 1:
            cov_ = np.cov(X, rowvar=False)  # Mean is removed
            _, evecs = np.linalg.eigh(cov_)
            evecs = np.fliplr(evecs)  # Sort by descending eigenvalues
            L = evecs.T  # Get as eigenvectors as rows
        else:
            L = np.eye(X.shape[1])

        if self.n_features_out is None:
            n_features_out = L.shape[0]
        else:
            n_features_out = self.n_features_out
            if L.shape[0] > n_features_out:
                warnings.warn('Decreasing the initial linear transformation '
                              'output dimensionality ({}) to the '
                              'preferred output dimensionality ({}).'.
                              format(L.shape[0], n_features_out),
                              DataDimensionalityWarning)
                L = L[:n_features_out]

        return L, n_features_out

    def _select_target_neighbors(self, X, y):
        """Find the target neighbors of each sample, that stay fixed during
        training.

        Parameters
        ----------
        X : array, shape (n_samples, n_features_in)
            The training samples.

        y : array, shape (n_samples,)
            The corresponding training labels indices.

        Returns
        -------
        target_neighbors: array, shape (n_samples, n_neighbors)
            An array of neighbors indices for each sample.
        """

        target_neighbors = np.empty((X.shape[0], self.n_neighbors_), dtype=int)
        for class_num in range(len(self.classes_)):
            class_ind, = np.where(np.equal(y, class_num))
            dist = euclidean_distances(X[class_ind], squared=True)
            np.fill_diagonal(dist, np.inf)
            neigh_ind = argpartition(dist, self.n_neighbors_ - 1, axis=1)
            neigh_ind = neigh_ind[:, :self.n_neighbors_]
            # argpartition doesn't guarantee sorted order, so we sort again
            # but only the k neighbors
            row_ind = np.arange(len(class_ind))[:, None]
            neigh_ind = neigh_ind[row_ind,
                                  np.argsort(dist[row_ind, neigh_ind])]
            target_neighbors[class_ind] = class_ind[neigh_ind]

        return target_neighbors

    @staticmethod
    def _compute_grad_static(X, targets):
        """Compute the gradient component due to the target neighbors that
        stays fixed throughout training

        Parameters
        ----------
        X : array, shape (n_samples, n_features_in)
            The training samples.

        Returns
        -------
        array, shape (n_features_in, n_features_in)
            An array with the sum of all weighted outer products.
        """

        n_samples, n_neighbors = targets.shape
        rows = np.repeat(np.arange(n_samples), n_neighbors)
        cols = targets.ravel()
        targets_sparse = csr_matrix((np.ones(n_samples * n_neighbors),
                                     (rows, cols)), shape=(n_samples,
                                                           n_samples))

        return sum_outer_products(X, targets_sparse)

    def _lbfgs_callback(self, L):
        self.n_iter_ += 1
        print('{:^9}'.format(self.n_iter_))

    def _loss_grad(self, L, X, y, targets, grad_static):
        """Compute the loss under a given linear transformation ``L`` and the
        loss gradient w.r.t. ``L``.

        Parameters
        ----------
        L : array, shape (n_features_out * n_features_in,)
            The current (flattened) linear transformation.

        X : array-like, shape (n_samples, n_features_in)
            The training samples.

        y : array, shape (n_samples,)
            The corresponding training labels.

        targets : array, shape (n_samples, n_neighbors)
            The target neighbors of each sample.

        grad_static : array, shape (n_features_in, n_features_in)
            The gradient component caused by target neighbors, that stays
            fixed throughout the algorithm.

        Returns
        -------
        loss: float
            The new loss.
        grad: array, shape (n_features_out * n_features_in,)
            The new (flattened) gradient.
        """

        n_samples, n_features_in = X.shape
        self.L_ = L.reshape(self.n_features_out_, n_features_in)

        tic = time.time()
        Lx = self.transform(X, check=False)

        # Compute distances to target neighbors under L (plus margin)
        dist_tn = np.zeros((n_samples, self.n_neighbors_))
        for k in range(self.n_neighbors_):
            dist_tn[:, k] = row_norms(Lx - Lx[targets[:, k]], squared=True) + 1

        # Compute distances to impostors under L
        margin_radii = np.add(dist_tn[:, -1], 2)

        imp1, imp2, dist_imp = self._find_impostors(Lx, y, margin_radii,
                                                    use_sparse=self.use_sparse)

        loss = 0
        A0 = csr_matrix((n_samples, n_samples))
        for k in reversed(range(self.n_neighbors_)):
            loss1 = np.maximum(dist_tn[imp1, k] - dist_imp, 0)
            act, = np.where(loss1 != 0)
            A1 = csr_matrix((2*loss1[act], (imp1[act], imp2[act])),
                            (n_samples, n_samples))

            loss2 = np.maximum(dist_tn[imp2, k] - dist_imp, 0)
            act, = np.where(loss2 != 0)
            A2 = csr_matrix((2*loss2[act], (imp1[act], imp2[act])),
                            (n_samples, n_samples))

            values = np.squeeze(np.asarray(A2.sum(0) + A1.sum(1).T))
            A0 = A0 - A1 - A2 + csr_matrix((values,
                                            (range(n_samples), targets[:, k])),
                                           (n_samples, n_samples))
            loss = loss + np.sum(loss1 ** 2) + np.sum(loss2 ** 2)

        grad_new = sum_outer_products(X, A0)
        grad = self.L_.dot(grad_static + grad_new)
        grad *= 2
        loss = loss + (grad_static * (self.L_.T.dot(self.L_))).sum()

        toc = time.time()
        self.n_funcalls_ += 1
        if self.verbose:
            print('{:9}\t{:^13}\t{:>20,.4f}\t{:8.2f}'
                  .format('', self.n_funcalls_, loss, toc-tic))

        if not os.path.exists('ires'):
            os.mkdir('ires')
        filename = os.path.join('ires', 'L_' + str(self.n_funcalls_))
        np.save(filename, arr=self.L_)

        return loss, grad.ravel()

    def _find_impostors(self, Lx, y, margin_radii, use_sparse=True):
        """Compute all impostor pairs exactly.

        Parameters
        ----------
        Lx : array, shape (n_samples, n_features_out)
            An array of transformed samples.

        y : array, shape (n_samples,)
            The corresponding class labels.

        margin_radii : array, shape (n_samples,)
            Distances to the farthest target neighbors + margin.

        use_sparse : bool, optional (default=True)
            Whether to use a sparse matrix for storing the impostor pairs.

        Returns
        -------
        imp1 : array, shape (n_impostors,)
            Sample indices.
        imp2 : array, shape (n_impostors,)
            Corresponding sample indices that violate a margin.
        dist : array, shape (n_impostors,)
            dist[i] is the distance between samples imp1[i] and imp2[i].
        """
        n_samples = Lx.shape[0]

        if use_sparse:
            # Initialize impostors matrix
            impostors_sp = csr_matrix((n_samples, n_samples), dtype=np.int8)

            for class_num in range(len(self.classes_) - 1):
                ind_in, = np.where(np.equal(y, class_num))
                ind_out, = np.where(np.greater(y, class_num))
                # print('\nFind imps for class {}'.format(class_num))
                # Subdivide ind_out x ind_in to chunks of a size that is
                # fitting in memory
                ii, jj = self._find_impostors_batch(Lx[ind_out], Lx[ind_in],
                                                    margin_radii[ind_out],
                                                    margin_radii[ind_in])

                if len(ii):
                    # sample constraints if they are too many
                    if len(ii) > self.max_constraints:
                        print('Sampling constraints...')
                        t_sample = time.time()
                        dims = (len(ind_out), len(ind_in))
                        ind = np.ravel_multi_index((ii, jj), dims=dims)
                        ind_samp = choice(ind, self.max_constraints,
                                          replace=False,
                                          random_state=self.random_state_)
                        ii, jj = np.unravel_index(ind_samp, dims=dims)
                        print('Sampled constraints in {:.4f}'.format(
                            time.time() - t_sample))

                    imp1 = ind_out[ii]
                    imp2 = ind_in[jj]
                    new_imps = csr_matrix(([1] * len(imp1), (imp1, imp2)),
                                          shape=(n_samples, n_samples),
                                          dtype=np.int8)
                    impostors_sp = impostors_sp + new_imps

            imp1, imp2 = impostors_sp.nonzero()
            # dist = row_norms(Lx[imp1] - Lx[imp2], True)
            dist = pairs_distances_batch(Lx, imp1, imp2)
        else:
            # Initialize impostors vectors
            imp1, imp2, dist = [], [], []
            for class_num in range(len(self.classes_) - 1):
                ind_in, = np.where(np.equal(y, class_num))
                ind_out, = np.where(np.greater(y, class_num))

                # Subdivide idx_out x idx_in to chunks of a size that is
                # fitting in memory
                ii, jj, dd = self._find_impostors_batch(
                    Lx[ind_out], Lx[ind_in], margin_radii[ind_out],
                    margin_radii[ind_in], return_dist=True)

                if len(ii):
                    # sample constraints if they are too many
                    if len(ii) > self.max_constraints:
                        print('Sampling constraints...')
                        dims = (len(ind_out), len(ind_in))
                        ind = np.ravel_multi_index((ii, jj), dims=dims)
                        ind_samp = choice(ind, self.max_constraints,
                                          replace=False,
                                          random_state=self.random_state_)
                        ii, jj = np.unravel_index(ind_samp, dims=dims)

                    imp1.extend(ind_out[ii])
                    imp2.extend(ind_in[jj])
                    # dist.extend(dd)

            imp1, imp2 = np.asarray(imp1), np.asarray(imp2)
            # dist = np.asarray(dist)
            dist = pairs_distances_batch(Lx, imp1, imp2)

        return imp1, imp2, dist

    @staticmethod
    def _find_impostors_batch(X1, X2, margin_radii1, margin_radii2,
                              return_dist=False, batch_size=5000):
        """Find impostor pairs in chunks to avoid large memory usage

        Parameters
        ----------
        X1 : array, shape (n_samples1, n_features)
            An array of transformed data samples.

        X2 : array, shape (n_samples2, n_features)
            Transformed data samples where n_samples2 < n_samples1.

        margin_radii1 : array, shape (n_samples1,)
            Distances of the samples in ``X1`` to their margins.

        margin_radii2 : array, shape (n_samples2,)
            Distances of the samples in ``X2`` to their margins.

        batch_size : int, optional (default=500)
            The size of each chunk of ``X1`` to compute distances to.

        return_dist : bool, optional (default=False)
            Whether to return the distances to the impostors.

        Returns
        -------
        imp1 : array, shape (n_impostors,)
            Sample indices.
        imp2 : array, shape (n_impostors,)
            Corresponding sample indices that violate a margin.
        dist : array, shape (n_impostors,), optional
            dist[i] is the distance between samples imp1[i] and imp2[i].
        """

        n_samples1 = X1.shape[0]
        imp1, imp2, dist = [], [], []

        # X2 squared norm stays constant, so pre-compute it to get a speed-up
        X2_norm_squared = row_norms(X2, squared=True)
        for chunk in gen_batches(n_samples1, batch_size):
            dist_out_in = euclidean_distances(X1[chunk], X2, squared=True,
                                              Y_norm_squared=X2_norm_squared)

            i1, j1 = np.where(dist_out_in < margin_radii1[chunk, None])
            i2, j2 = np.where(dist_out_in < margin_radii2[None, :])

            ind1 = np.ravel_multi_index((i1, j1), dist_out_in.shape)
            ind2 = np.ravel_multi_index((i2, j2), dist_out_in.shape)
            ind = np.unique(np.concatenate((ind1, ind2)))

            if len(ind):
                ii, jj = np.unravel_index(ind, dist_out_in.shape)
                imp1.extend(ii + chunk.start)
                imp2.extend(jj)
                if return_dist:
                    dist.extend(dist_out_in[ii, jj])

        if return_dist:
            return imp1, imp2, dist
        else:
            return imp1, imp2

    def _energy_classify_batch(self, X_train, y_train, X_test, batch_size=500):
        """Assign labels to test samples based on energy minimization in
        chunks to avoid large memory usage.

        Parameters
        ----------
        X_train : array, shape (n_samples_train, n_features)
            The array of the original training samples.

        y_train : array, shape (n_samples_train,)
            The corresponding training labels.

        X_test : array, shape (n_samples_test, n_features)
            Test data samples.

        batch_size : int, optional (default=500)
            The size of each chunk of ``X1`` to compute distances to.

        Returns
        -------
        y_pred : array, shape (n_samples_test,)
            Predicted labels.
        """

        n_samples_train = X_train.shape[0]
        if n_samples_train >= 50000:
            batch_size = batch_size // 2
        else:
            batch_size = 5000
        n_samples_test = X_test.shape[0]

        targets = self._select_target_neighbors(X_train, y_train)

        Lx_tr = self.transform(X_train)
        Lx_test = self.transform(X_test)

        classes = np.unique(y_train)
        n_classes = len(classes)
        y_pred = np.zeros(n_samples_test)

        K = self.n_neighbors_
        dist_train_tn = np.zeros((n_samples_train, K))
        for k in range(K):
            dist_train_tn[:, k] = row_norms(Lx_tr - Lx_tr[targets[:, k]],
                                            squared=True) + 1

        for chunk in gen_batches(n_samples_test, batch_size):
            dist_train_test = euclidean_distances(Lx_tr, Lx_test[chunk],
                                                  squared=True)

            energy_chunk = np.zeros((chunk.stop-chunk.start, n_classes))
            for class_num, test_label in enumerate(classes):
                ind_friend, = np.where(np.equal(y_train, test_label))
                ind_enemy, = np.where(np.not_equal(y_train, test_label))

                dist_enemy_tn = dist_train_tn[ind_enemy]
                dist_test_enemy = dist_train_test[ind_enemy].T
                dist_test_tn = partition(dist_train_test[ind_friend].T, K-1, 1)
                dist_test_tn = np.sort(dist_test_tn, axis=1)

                # static term: distance to target neighbors
                energy_chunk[:, class_num] = dist_test_tn.sum(axis=1)

                for k in range(K):
                    # hinge loss 1: from test inputs that are impostors
                    dist_enemy_tn_k = dist_enemy_tn[:, k]
                    dist_hinge_1 = dist_enemy_tn_k[:, None] - dist_test_enemy.T
                    hinge_1 = np.maximum(dist_hinge_1, 0).sum(axis=0)
                    energy_chunk[:, class_num] += hinge_1

                    # hinge loss 2: from impostors to test inputs
                    dist_test_tn_k = dist_test_tn[:, k]
                    dist_hinge_2 = dist_test_tn_k[:, None] - dist_test_enemy
                    hinge_2 = np.maximum(dist_hinge_2, 0).sum(axis=1)
                    energy_chunk[:, class_num] += hinge_2

            y_pred[chunk] = classes[np.argmin(energy_chunk, axis=1)]

        return y_pred


##########################
# Some helper functions #
#########################


def check_scalar(x, name, dtype, min_val=None, max_val=None):
    """Validates scalar parameters by checking if their datatype matches and
    if their values are within a valid given range.

    Parameters
    ----------
    x : object
        The scalar parameter to validate.

    name : str
        The name of the parameter to be printed in error messages.

    dtype : type
        The desired datatype for the parameter

    min_val : float or int, optional (default=None)
        The minimum value value the parameter can take. If None (default) it
        is implied that the parameter does not have a lower bound.

    max_val: float or int, optional (default=None)
        The maximum valid value the parameter can take. If None (default) it
        is implied that the parameter does not have an upper bound.

    Raises
    -------
    TypeError
        If the parameter's type does not match the desired type.

    ValueError
        If the parameter's value violates the given bounds.
    """

    if type(x) is not dtype:
        raise TypeError('{} must be {}.'.format(name, dtype))

    if min_val is not None and x < min_val:
        raise ValueError('{} must be >= {}.'.format(name, min_val))

    if max_val is not None and x > max_val:
        raise ValueError('{} must be <= {}.'.format(name, max_val))


def sum_outer_products(X, weights):
    """Computes the sum of weighted outer products using a sparse weights
    matrix

    Parameters
    ----------
    X : array, shape (n_samples, n_features_in)
        An array of data samples.

    weights : csr_matrix, shape (n_samples, n_samples)
        A sparse weights matrix (indicating target neighbors).


    Returns
    -------
    sum_outer_prods : array, shape (n_features_in, n_features_in)
        The sum of all weighted outer products.
    """

    weights_sym = weights + weights.T
    n_samples = weights_sym.shape[0]
    diag = spdiags(weights_sym.sum(axis=0), 0, n_samples, n_samples)
    laplacian = diag.tocsr() - weights_sym
    sum_outer_prods = X.T.dot(laplacian.dot(X))

    return sum_outer_prods


def pairs_distances_batch(X, ind_a, ind_b, batch_size=5000):
    """Equivalent to  np.sum(np.square(X[ind_a] - X[ind_b]), axis=1)

    Parameters
    ----------
    X : array, shape (n_samples, n_features_in)
        An array of data samples.

    ind_a : array, shape (n_indices,)
        An array of sample indices.

    ind_b : array, shape (n_indices,)
        Another array of sample indices.

    batch_size : bool, optional (default=500)
        Size of each chunk of ``X`` to compute distances for.

    Returns
    -------
    dist: array, shape (n_indices,)
        An array of pairwise distances.
    """

    n_indices = len(ind_a)
    dist = np.zeros(n_indices)
    for chunk in gen_batches(n_indices, batch_size):
        dist[chunk] = row_norms(X[ind_a[chunk]] - X[ind_b[chunk]], True)

    return dist


if __name__ == '__main__':
    import doctest
    doctest.testmod()