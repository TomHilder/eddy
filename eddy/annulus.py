"""Simple class to deproject spectra and measure the rotation velocity."""

import celerite
import numpy as np
from scipy.stats import binned_statistic
from scipy.optimize import curve_fit
from scipy.optimize import minimize_scalar
from scipy.interpolate import interp1d


class ensemble(object):

    def __init__(self, spectra, theta, velax, suppress_warnings=True,
                 remove_empty=True, sort_spectra=True):
        """Initialize the class."""

        # Suppress warnings.
        if suppress_warnings:
            import warnings
            warnings.filterwarnings("ignore")

        # Read in the spectra and remove empty values.
        self.theta = theta
        self.spectra = spectra

        # Sort the spectra.
        if sort_spectra:
            idxs = np.argsort(self.theta)
            self.spectra = self.spectra[idxs]
            self.theta = self.theta[idxs]

        # Remove empty pixels.
        if remove_empty:
            idxs = np.sum(spectra, axis=-1) > 0.0
            self.theta = self.theta[idxs]
            self.spectra = self.spectra[idxs]

        # Easier to use variables.
        self.theta_deg = np.degrees(theta) % 360.
        self.spectra_flat = spectra.flatten()

        # Check there's actually spectra.
        if self.theta.size < 1:
            raise ValueError("No finite spectra. Check for NaNs.")

        # Velocity axis.
        self.velax = velax
        self.channel = np.diff(velax)[0]
        self.velax_range = (self.velax[0] - 0.5 * self.channel,
                            self.velax[-1] + 0.5 * self.channel)
        self.velax_mask = np.percentile(self.velax, [30, 70])

    # -- Rotation Velocity by Gaussian Process Modelling -- #

    def get_vrot_GP(self, vref=None, p0=None, resample=False, optimize=True,
                    nwalkers=64, nburnin=300, nsteps=300, scatter=1e-3,
                    plot_walkers=True, plot_corner=True, return_all=False,
                    **kwargs):
        """Infer the rotation velocity of the annulus by finding the velocity
        which after deprojecting the spectra to a common velocity produces the
        smoothest spectrum.

        Args:
            vref (Optional[float]): Predicted rotation velocity, typically the
                Keplerian velocity at that radius. Will be used as bounds for
                searching for the true velocity, set as +\- 30%.
            p0 (Optional[list]): Initial parameters for the minimization. Will
                override any guess for vref.
            resample (Optional[bool]): Resample the shifted spectra by this
                factor. For example, resample = 2 will shift and bin the
                spectrum down to sampling rate twice that of the original data.
                Not recommended for the GP approach.
            optimize (Optional[bool]): Optimize the starting positions before
                the MCMC runs. If an integer, the number of iterations to use
                of optimization.
            nwalkers (Optional[int]): Number of walkers used for the MCMC runs.
            nburnin (Optional[int]): Number of steps used to burn in walkers.
            nsteps (Optional[int]): Number of steps taken to sample posteriors.
            scatter (Optional[float]): Scatter applied to the starting
                positions before running the MCMC.
            plot_walkers (Optional[bool]): Plot the trace of the walkers.
            plot_corner (Optional[bool]): Plot the covariances of the
                posteriors using corner.py.
            return_all (Optional[bool]): If True, return the percentiles of the
                posterior distributions for all parameters, otherwise just the
                percentiles for the rotation velocity.
            **kwargs (Optional[dict]): Additional kwargs to pass to minimize.

        Returns:
            percentiles (ndarray): The 16th, 50th and 84th percentiles of the
                posterior distribution for all four parameters if return_all is
                True otherwise just for the rotation velocity.

        """

        # Import emcee for the MCMC.
        import emcee
        if resample:
            print("WARNING: Resampling with the GP method is not advised.")

        # Initialize the starting positions.
        if vref is not None and p0 is not None:
            print("WARNING: Initial value of p0 (%.2f) " % (p0[0]) +
                  "used in place of vref (%.2f)." % (vref))
        if vref is not None:
            if not isinstance(vref, (int, float)):
                vref = vref[0]
        else:
            vref = self.guess_parameters(fit=True)[0]
        p0 = self._guess_parameters_GP(vref) if p0 is None else p0
        if len(p0) != 4:
            raise ValueError('Incorrect length of p0.')
        vref = p0[0]

        # Optimize if necessary.
        if optimize:
            p0 = self._optimize_p0(p0, N=optimize, resample=resample, **kwargs)
            vref = p0[0]
        p0 = ensemble._randomize_p0(p0, nwalkers, scatter)
        if np.any(np.isnan(p0)):
            raise ValueError("WARNING: NaNs in the p0 array.")

        # Set up emcee.
        sampler = emcee.EnsembleSampler(nwalkers, 4, self._lnprobability,
                                        args=(vref, resample))

        # Run the sampler.
        sampler.run_mcmc(p0, nburnin + nsteps)
        samples = sampler.chain[:, -nsteps:]
        samples = samples.reshape(-1, samples.shape[-1])

        # Diagnosis plots if appropriate.
        if plot_walkers:
            ensemble._plot_walkers(sampler, nburnin)
        if plot_corner:
            ensemble._plot_corner(samples)

        # Return the perncetiles.
        percentiles = np.percentile(samples, [16, 50, 84], axis=0)
        if return_all:
            return percentiles
        return percentiles[:, 0]

    def _optimize_p0(self, p0, N=1, resample=True, **kwargs):
        """
        Optimize the starting positions, p0. We do this in a slightly hacky way
        because the minimum is not easily found. We first optimize the hyper
        parameters of the GP model, holding the rotation velocity constant,
        then, holding the GP hyperparameters constant, optimizing the rotation
        velocity, before optimizing everything together. This can be run
        multiple times to iteratie to a global optimum.

        Args:
            p0 (ndarray): Initial guess of the starting positions.
            N (Optional[int]): Interations of the optimization to run.
            resample (Optional[bool/int]): If true, resample the deprojected
                spectra donw to the original velocity resolution. If an integer
                is given, use this as the bew sampling rate relative to the
                original data.

        Returns:
            p0 (ndarray): Optimized array. If scipy.minimize does not converge
                then p0 will not be updated. No warnings are given, however.
        """
        from scipy.optimize import minimize
        kwargs['method'] = kwargs.get('method', 'L-BFGS-B')
        kwargs['options'] = {'maxiter': 100000, 'maxfun': 100000, 'ftol': 1e-4}

        # Cycle through the required number of iterations. Only update p0 if
        # both the minimization converged (res.success == True) and there is an
        # improvement in the likelihood.

        nlnL = self._negative_lnlikelihood(p0, resample=resample)

        for i in range(int(N)):

            # Define the bounds.
            bounds = (0.8 * p0[0], 1.2 * p0[0])
            bounds = [bounds, (0.0, None), (-15.0, 10.0), (0.0, 10.0)]

            # First minimize hyper parameters, holding vrot constant.
            res = minimize(self._negative_lnlikelihood_hyper, x0=p0[1:],
                           args=(p0[0], resample), bounds=bounds[1:],
                           **kwargs)
            if res.success:
                p0_temp = p0
                p0_temp[1:] = res.x
                nlnL_temp = self._negative_lnlikelihood(p0_temp, resample)
                if nlnL_temp < nlnL:
                    p0 = p0_temp
                    nlnL = nlnL_temp

            # Second, minimize vrot holding the hyper parameters constant.
            res = minimize(self._negative_lnlikelihood_vrot, x0=p0[0],
                           args=(p0[1:], resample), bounds=[bounds[0]],
                           **kwargs)
            if res.success:
                p0_temp = p0
                p0_temp[0] = res.x
                nlnL_temp = self._negative_lnlikelihood(p0_temp, resample)
                if nlnL_temp < nlnL:
                    p0 = p0_temp
                    nlnL = nlnL_temp

            # Final minimization with everything.
            res = minimize(self._negative_lnlikelihood, x0=p0,
                           args=(resample), bounds=bounds,
                           **kwargs)
            if res.success:
                p0_temp = res.x
                nlnL_temp = self._negative_lnlikelihood(p0_temp, resample)
                if nlnL_temp < nlnL:
                    p0 = p0_temp
                    nlnL = nlnL_temp

        return p0

    def _guess_parameters_GP(self, vref, fit=True):
        """Guess the starting positions from the spectra."""
        vref = self.guess_parameters(fit=fit)[0] if vref is None else vref
        noise = int(min(10, self.spectra.shape[1] / 3.0))
        noise = np.std([self.spectra[:, :noise], self.spectra[:, -noise:]])
        ln_sig = np.log(np.std(self.spectra))
        ln_rho = np.log(150.)
        return np.array([vref, noise, ln_sig, ln_rho])

    @staticmethod
    def _randomize_p0(p0, nwalkers, scatter):
        """Estimate (vrot, noise, lnp, lns) for the spectrum."""
        dp0 = np.random.randn(nwalkers * len(p0)).reshape(nwalkers, len(p0))
        dp0 = np.where(p0 == 0.0, 1.0, p0)[None, :] * (1.0 + scatter * dp0)
        return np.where(p0[None, :] == 0.0, dp0 - 1.0, dp0)

    def _negative_lnlikelihood_vrot(self, vrot, hyperparams, resample=False):
        """Negative log-likelihood function with vrot as only variable."""
        theta = np.insert(hyperparams, 0, vrot)
        nll = -self._lnlikelihood(theta, resample)
        return nll if np.isfinite(nll) else 1e15

    def _negative_lnlikelihood_hyper(self, hyperparams, vrot, resample=False):
        """Negative log-likelihood function with hyperparams as variables."""
        theta = np.insert(hyperparams, 0, vrot)
        nll = -self._lnlikelihood(theta, resample)
        return nll if np.isfinite(nll) else 1e15

    def _negative_lnlikelihood(self, theta, resample=False):
        """Negative log-likelihood function for optimization."""
        nll = -self._lnlikelihood(theta, resample)
        return nll if np.isfinite(nll) else 1e15

    @staticmethod
    def _build_kernel(x, y, theta):
        """Build the GP kernel. Returns None if gp.compute(x) fails."""
        noise, lnsigma, lnrho = theta[1:]
        k_noise = celerite.terms.JitterTerm(log_sigma=np.log(noise))
        k_line = celerite.terms.Matern32Term(log_sigma=lnsigma, log_rho=lnrho)
        gp = celerite.GP(k_noise + k_line, mean=np.nanmean(y), fit_mean=True)
        try:
            gp.compute(x)
        except Exception:
            return None
        return gp

    def _get_masked_spectra(self, theta, resample=True):
        """Return the masked spectra for fitting."""
        vrot = theta[0]
        x, y = self.deprojected_spectrum(vrot, resample=resample)
        mask = np.logical_and(x >= self.velax_mask[0], x <= self.velax_mask[1])
        return x[mask], y[mask]

    @staticmethod
    def _lnprior(theta, vref):
        """Uninformative log-prior function for MCMC."""
        vrot, noise, lnsigma, lnrho = theta
        if abs(vrot - vref) / vref > 0.2:
            return -np.inf
        if vrot <= 0.0:
            return -np.inf
        if noise <= 0.0:
            return -np.inf
        if not -15.0 < lnsigma < 10.:
            return -np.inf
        if not 0.0 <= lnrho <= 10.:
            return -np.inf
        return 0.0

    def _lnlikelihood(self, theta, resample=False):
        """Log-likelihood function for the MCMC."""

        # Deproject the data and resample if requested.
        x, y = self._get_masked_spectra(theta, resample=resample)

        # Build the GP model and calculate the log-likelihood.
        gp = ensemble._build_kernel(x, y, theta)
        if gp is None:
            return -np.inf
        ll = gp.log_likelihood(y, quiet=True)
        return ll if np.isfinite(ll) else -np.inf

    def _lnprobability(self, theta, vref, resample=False):
        """Log-probability function for the MCMC."""
        if ~np.isfinite(ensemble._lnprior(theta, vref)):
            return -np.inf
        return self._lnlikelihood(theta, resample)

    # -- Rotation Velocity by Minimizing Linewidth -- #

    def get_vrot_dV(self, vref=None, resample=False):
        """Infer the rotation velocity by finding the rotation velocity which,
        after shifting all spectra to a common velocity, results in the
        narrowest stacked profile.

        Args:
            vref (Optional[float]): Predicted rotation velocity, typically the
                Keplerian velocity at that radius. Will be used as the starting
                position for the minimization.
            resample (Optional[bool]): Resample the shifted spectra by this
                factor. For example, resample = 2 will shift and bin the
                spectrum down to sampling rate twice that of the original data.

        Returns:
            vrot (float): Rotation velocity which minimizes the width.

        """
        vref = self.guess_parameters(fit=True)[0] if vref is None else vref
        bounds = np.array([0.7, 1.3]) * vref
        res = minimize_scalar(self.get_deprojected_width, method='bounded',
                              bounds=bounds, args=(resample))
        return res.x if res.success else np.nan

    @staticmethod
    def _get_p0_gaussian(x, y):
        """Estimate (x0, dV, Tb) for the spectrum."""
        if x.size != y.size:
            raise ValueError("Mismatch in array shapes.")
        Tb = np.max(x)
        x0 = x[y.argmax()]
        dV = np.trapz(y, x) / Tb / np.sqrt(2. * np.pi)
        return x0, dV, Tb

    @staticmethod
    def _fit_gaussian(x, y, dy=None, return_uncertainty=False):
        """Fit a gaussian to (x, y, [dy])."""
        try:
            popt, cvar = curve_fit(ensemble._gaussian, x, y, sigma=dy,
                                   p0=ensemble._get_p0_gaussian(x, y),
                                   absolute_sigma=True, maxfev=100000)
            cvar = np.diag(cvar)
        except Exception:
            popt = [np.nan, np.nan, np.nan]
            cvar = popt.copy()
        if return_uncertainty:
            return popt, cvar
        return popt

    @staticmethod
    def _get_gaussian_width(x, y, fill_value=1e50):
        """Return the absolute width of a Gaussian fit to the spectrum."""
        dV = ensemble._fit_gaussian(x, y)[1]
        if np.isfinite(dV):
            return abs(dV)
        return fill_value

    @staticmethod
    def _get_gaussian_center(x, y):
        """Return the line center from a Gaussian fit to the spectrum."""
        x0 = ensemble._fit_gaussian(x, y)[0]
        if np.isfinite(x0):
            return abs(x0)
        return x[np.argmax(y)]

    def get_deprojected_width(self, vrot, resample=True):
        """Return the spectrum from a Gaussian fit."""
        x, y = self.deprojected_spectrum(vrot, resample=resample)
        mask = np.logical_and(x >= self.velax[0], x <= self.velax[-1])
        return ensemble._get_gaussian_width(x[mask], y[mask])

    # -- Line Profile Functions -- #

    @staticmethod
    def _gaussian(x, x0, dV, Tb):
        """Gaussian function."""
        return Tb * np.exp(-np.power((x - x0) / dV, 2.0))

    @staticmethod
    def _thickline(x, x0, dV, Tex, tau):
        """Optically thick line profile."""
        if tau <= 0.0:
            raise ValueError("Must have positive tau.")
        return Tex * (1. - np.exp(-ensemble._gaussian(x, x0, dV, tau)))

    @staticmethod
    def _SHO(x, A, y0):
        """Simple harmonic oscillator."""
        return A * np.cos(x) + y0

    @staticmethod
    def _SHOb(x, A, y0, dx):
        """Simple harmonic oscillator with offset."""
        return A * np.cos(x + dx) + y0

    # -- Deprojection Functions -- #

    def deprojected_spectra(self, vrot):
        """Returns (x, y) of all deprojected points as an ensemble."""
        spectra = []
        for theta, spectrum in zip(self.theta, self.spectra):
            shifted = interp1d(self.velax - vrot * np.cos(theta), spectrum,
                               bounds_error=False, fill_value=np.nan)
            spectra += [shifted(self.velax)]
        return np.squeeze(spectra)

    def deprojected_spectrum(self, vrot, resample=False):
        """Returns (x, y) of collapsed deprojected spectrum."""
        vpnts = self.velax[None, :] - vrot * np.cos(self.theta)[:, None]
        vpnts, spnts = self._order_spectra(vpnts=vpnts.flatten())
        return self._resample_spectra(vpnts, spnts, resample=resample)

    def deprojected_spectrum_maximum(self, resample=False, method='quadratic'):
        """Deprojects data such that their max values are aligned."""
        vmax = self.peak_velocities(method=method)
        vpnts = np.array([self.velax - dv for dv in vmax - np.median(vmax)])
        vpnts, spnts = self._order_spectra(vpnts=vpnts.flatten())
        return self._resample_spectra(vpnts, spnts, resample=resample)

    def peak_velocities(self, method='quadratic'):
        """
        Return the velocities of the peak pixels.

        Args:
            method (str): Method used to determine the line centroid. Must be
                in ['max', 'quadratic', 'gaussian']. The former returns the
                pixel of maximum value, 'quadratic' fits a quadratic to the
                pixel of maximum value and its two neighbouring pixels (see
                Teague & Foreman-Mackey 2018 for details) and 'gaussian' fits a
                Gaussian profile to the line.

        Returns:
            vmax (ndarray): Line centroids.
        """
        method = method.lower()
        if method == 'max':
            vmax = np.take(self.velax, np.argmax(self.spectra, axis=1))
        elif method == 'quadratic':
            from bettermoments.methods import quadratic
            vmax = [quadratic(spectrum, x0=self.velax[0], dx=self.channel)[0]
                    for spectrum in self.spectra]
            vmax = np.array(vmax)
        elif method == 'gaussian':
            vmax = [ensemble._get_gaussian_center(self.velax, spectrum)
                    for spectrum in self.spectra]
            vmax = np.array(vmax)
        else:
            raise ValueError("method is not 'max', 'gaussian' or 'quadratic'.")
        return vmax

    def _order_spectra(self, vpnts, spnts=None):
        """Return velocity order spectra."""
        spnts = self.spectra_flat if spnts is None else spnts
        if len(spnts) != len(vpnts):
            raise ValueError("Wrong size in 'vpnts' and 'spnts'.")
        idxs = np.argsort(vpnts)
        return vpnts[idxs], spnts[idxs]

    def _resample_spectra(self, vpnts, spnts, resample=False):
        """Resample the spectra."""
        if not resample:
            return vpnts, spnts
        bins = int((self.velax.size - 1) * resample)
        y, x_edges = binned_statistic(vpnts, spnts, statistic='mean',
                                      bins=bins, range=self.velax_range)[:2]
        return np.average([x_edges[1:], x_edges[:-1]], axis=0), y

    def guess_parameters(self, fit=True, fix_theta=True, method='quadratic'):
        """Guess vrot and vlsr from the spectra.."""
        vpeaks = self.peak_velocities(method=method)
        vrot = 0.5 * (np.max(vpeaks) - np.min(vpeaks))
        vlsr = np.mean(vpeaks)
        if not fit:
            return vrot, vlsr
        try:
            if fix_theta:
                return curve_fit(ensemble._SHO, self.theta, vpeaks,
                                 p0=[vrot, vlsr], maxfev=10000)[0]
            else:
                return curve_fit(ensemble._SHOb, self.theta, vpeaks,
                                 p0=[vrot, vlsr, 0.0], maxfev=10000)[0]
        except Exception:
            return vrot, vlsr

    # -- Plotting Functions -- #

    def plot_spectra(self, ax=None):
        """Plot all the spectra."""
        ax = ensemble._make_axes(ax)
        for spectrum in self.spectra:
            ax.step(self.velax, spectrum, where='mid', color='k')
        ax.set_xlabel('Velocity')
        ax.set_ylabel('Intensity')
        ax.set_xlim(self.velax[0], self.velax[-1])
        return ax

    def plot_river(self, vrot=None, ax=None, xlims=None, ylims=None,
                   plot_max=True):
        """Plot a river plot."""
        raise NotImplementedError("No. Not now.")
        if vrot is None:
            toplot = self.spectra
        else:
            toplot = self.deprojected_spectra(vrot)
        toplot /= np.nanmax(toplot, axis=1)[:, None]
        toplot = np.where(np.isnan(toplot), 0.0, toplot)
        ax = ensemble._make_axes(ax)
        ax.imshow(toplot, origin='lower', interpolation='nearest',
                  aspect='auto', vmin=0.0, vmax=1.0,
                  extent=[self.velax.min(), self.velax.max(),
                          self.theta.min(), self.theta.max()])
        if plot_max:
            ax.errorbar(np.take(self.velax, np.argmax(toplot, axis=1)),
                        self.theta, color='k', fmt='o', mew=0.0, ms=2)
        if xlims is not None:
            ax.set_xlim(xlims[0], xlims[1])
        if ylims is not None:
            ax.set_ylim(ylims[0], ylims[1])
        ax.set_xlabel(r'${\rm Velocity \, (m\,s^{-1})}$')
        ax.set_ylabel(r'${\rm Polar \,\, Angle \quad (rad)}$')
        return ax

    @staticmethod
    def _make_axes(ax):
        """Make an axis to plot on."""
        import matplotlib.pyplot as plt
        if ax is None:
            _, ax = plt.subplots()
        return ax

    @staticmethod
    def _plot_corner(samples):
        """Plot the corner plot for the MCMC."""
        import corner
        labels = [r'${\rm v_{rot}}$', r'${\rm \sigma_{rms}}$',
                  r'${\rm ln(\sigma)}$', r'${\rm ln(\rho)}$']
        corner.corner(samples, labels=labels, quantiles=[0.16, 0.5, 0.84],
                      show_titles=True)

    @staticmethod
    def _plot_walkers(sampler, nburnin):
        """Plot the walkers from the MCMC."""
        import matplotlib.pyplot as plt
        labels = [r'${\rm v_{rot}}$', r'${\rm \sigma_{rms}}$',
                  r'${\rm ln(\sigma)}$', r'${\rm ln(\rho)}$']
        for s, sample in enumerate(sampler.chain.T):
            _, ax = plt.subplots()
            for walker in sample.T:
                ax.plot(walker, alpha=0.1, color='k')
            ax.set_xlabel('Steps')
            ax.set_ylabel(labels[s])
            ax.axvline(nburnin, ls=':', color='k')
