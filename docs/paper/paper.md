---
title: 'Eddy: Extracting Disk Dynamics'
tags:
  - Python
  - astronomy
  - dynamics
  - accretion disk dynamics
  - protoplanetary disks
authors:
  - name: Richard Teague
    orcid: 0000-0003-1534-5186
    affiliation: "1"
affiliations:
 - name: University of Michigan
   index: 1
date: 21 January 2019
bibliography: paper.bib
---

# Summary

``eddy`` is a Python package which implements several methods for extracting kinematical information from astronomical observations of Doppler shifted molecular line emission in protoplanetary disks, the birthplace of planets. Such kinematic information is essential for constraining the physical structure of the accretion disk and searching for evidence of embedded planets [@Teague:2018; @Teague:2018b]. Basic functionality includes two types of analysis: either for full 2D rotation maps ("first moment maps"), or for a 1D annulus of spectra extracted from a particular radius in the disk.

Fitting a first-moment map is a frequently used analysis in the study of protoplanetary disks, typically used to constrain the mass of the central star. `eddy` includes the ability to have a 3D geometry which is becoming increasingly more important with high spatial resolution observations as the disks are clearly no longer flat, 2D structures. A simple dictionary interface allows the user to vary specific parameters in the fitting while holding well constrained values fixed.

``eddy`` also implements the methods described in [@Teague:2018] and [@Teague:2018b] to infer the rotation velocity based on the relative shift of spectra from different regions in the disk.

There are several Jupyter Notebooks of examples of how to use of the the specific functions.

# Acknowledgements

We acknowledge Dan Foreman-Mackey for helping with the development of the implementation and Til Birnstiel for the development of the documentation.

# References