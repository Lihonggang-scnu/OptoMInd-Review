# Reading View

<!-- snapshot_id: snapshot-08c038bb8496aee2485270fa -->
> Material depth: `fulltext`
> This view is a structured material projection; asset captions do not prove image interpretation.

> Known gaps:
> - identity: source identity was not independently verified (status=input_attributed)
> - formula: formula region has no recognized formula; flattened orig text is not a faithful formula (status=recognition_required)
> - parser_provenance: local model weight hashes were not recorded; package versions, declared model/options and raw output hashes are retained (status=unknown)
> - bibliography: Docling did not label individual references; the bibliography text is retained in ordinary source order, not normalized reference identities (status=unstructured)
> - pdf: ordinary body and region index from native Docling; regional enhancement is independent (status=docling_basic)

> Auxiliary metadata retained separately from publisher body:
> - observed_metadata: sources/observed-metadata-ca08f1f548ed.json; use only as supplied metadata/snippet evidence

## Metalenses at visible wavelengths: past, present, perspectives

## [block-7bd999e16c7af97cacf2] Metalenses at visible wavelengths: past, present, perspectives

[block-6b53c1581775d11ca83b] Authors: Philippe Lalanne 1 *, Pierre Chavel 2,3 * 1 Laboratoire Photonique, Numérique et Nanosciences (LP2N), IOGS  Univ. Bordeaux  CNRS, 33400 Talence cedex, France. 2 Laboratoire Charles Fabry (LCF), IOGS  CNRS  Univ. Paris-Saclay, 2 avenue Augustin Fresnel, 91127 Palaiseau cedex, France. 3 Laboratoire Hubert Curien, Université de Lyon, Univ. Jean Monnet de Saint Etienne, CNRS UMR 5516, 42000 Saint Etienne, France *Correspondence to: philippe.lalanne@institutoptique.fr, pierre.chavel@institutoptique.fr Abstract: The so-called 'flat optics' that shape the amplitude and phase of light with high spatial resolution are presently receiving considerable attention. Numerous publications in high-impact journals seemingly offer hope for great promises for ultra-flat metalenses with high efficiency, high numerical aperture, broadband operation... We temperate the expectation by referring the current status of metalenses against their historical background, assessing the technical and scientific challenges recently solved and critically identifying those that still stand in the way.

[block-2e97a6eff2bd2a9126ca] Introduction

[block-b2921dc0c65b614ed532] Recently, nanostructured metasurfaces have received increasing attention due to their ability to control the amplitude, phase, polarization, orbital momentum, absorption, reflectance, emissivity of light with high spatial resolution. This leads scientists and engineers to revisit various traditional applications in optics from a different angle. The broad panel of researches on metasurfaces, already well documented in excellent review articles [Zhu16, Kuz16], is not the topic of this expert opinion. We focus on metalenses hereafter, and since low numerical aperture (NA) lenses have been fabricated at low cost with remarkable performance for a long time, we restrict the discussion to high-NA, high efficiencies metalenses. Since the publication of a research article in Science that revisited Snell's law at the interface between two uniform media thanks to an ultrathin grating composed of metallic nanoantennas etched on the interface [Yu11], the field of metalenses has been booming. A recent flagship research article [Kho16] that reports metalenses manufactured with a high NA operating at visible wavelengths is particularly emblematic of the growing interest. Comparisons with a state-of-the-art commercial objectives suggest that the image quality is as good and even better, and considering their flat nature and compact size, metalenses appear as potentially revolutionary. This new potential is all the more significant because classical échelette diffractive lenses present a ridiculously small efficiency for large NA [Swa89] , precluding their use for high resolution focusing, except for the ancillary function of chromatic aberration correction where the local spatial frequency is typically much smaller. However, metalenses do not come out of the blue and the so-called 'flat optics' that shapes the phase of free-space waves through subwavelength structures have a venerable history over many years from the microwave domain down to the visible. What is the paradigm change at the source of this anticipated revolution? New fabrication processes, new concepts, new applications, fundamental limitation shifts?

[block-42952c69a6fdf0453edd] To answer these questions, we confront recent metalens achievements with an historical perspective of flat optical elements using subwavelength structures, to further analyze the fundamental limitations that have been lifted and better anticipate perspectives offered by flat subwavelength optics. This is precisely the outline of the present article, which starts with a brief overview of the fundamentals of flat optics and emphasizes approaches that offer both high numerical aperture and high diffraction efficiency, and potentially broadband operation.

## [block-64667178ef2427e99e0b] 2. Flat-optic basis

[block-f91117d1338bfd92ac5c] The basic role of optical components such as lenses or prisms is to shape incoming wavefronts. In recent years, various flat-optics components with an optical thickness on the order of the wavelength have been introduced and demonstrated. In contrast to bulk optics, where phase is modulated by a continuous variation of the optical thickness, flat optics restrict the modulation to its minimum, 2𝜋, by wrapping the bulk-optics phase modulo 2𝜋 for a nominal design wavelength 𝜆. The wavefront is divided into zones wherein the phase varies by 2𝜋, with a 2𝜋 discontinuity between them. At 𝜆, the diffraction patterns of the various zones combine coherently to form exactly the initial continuous wave, aside from border effects that are negligible if the zone widths are much larger than 𝜆. At other wavelengths, the wrapping procedure introduces a specific dispersion effect that splits the wavefront into a set of orders and therefore reduces the diffraction efficiency of the desired order [Swa89]. Compared to travelling the same distance in air (of index assumed to be unity), the optical path delay for a zone thickness 𝑡 is Δ = (𝑛 -1)𝑡. [1] Conventional diffractive optical components [Aur72], such as the well-known "échelette" blazed grating, implement a 2𝜋-phase delay across the component surface by a 𝜆 (𝑛-1) / thickness excursion

[block-7a2bb50408768cdd0bef] of the diffractive surface profile, see Fig. 1.

[block-d2a54042c083f12fb64f] Figure image region; pixels have not been interpreted.

[block-02844e8b3039482825d2] Fig. 1. Echelette-type diffractive optical elements blazed in the first order. Echelette lenses are often called Fresnel lenses, although the historical lighthouse Fresnel lens is essentially a bulk optical component. For a nominal wavelength 𝜆, the Fresnel lens is said to be blazed into the 𝑚th order if the small facet thickness 𝐻 of every échelette is 𝑚𝜆 (𝑛-1) / , with 𝑛 being the refractive index of the lens material.

[block-8bcaf3fe2433fb156f0b] Nowadays, échelette-type diffractive optical elements are manufactured at low-cost by replication technologies such as embossing, molding and casting. Those technologies offer very high resolution, typically in the nanometer range, and allow the fabrication of large area, complex microstructures with minimal light loss in durable materials through high volume industrial production processes [Gal97]. However, they suffer two main limitations:

[block-c49e3bd88e30251d93d9]  The discontinuities of the wrapped phase introduce a shadow that wastes light into spurious orders. The effect is stringent for short period échelettes, to an extent that the échelette approach is essentially useless for high-NA lenses.

[block-c71f4b6ff094beb17e81]  The wrapping is valid a nominal design wavelength 𝜆, implying that the imprinted phase varies with the wavelength of the incident light. Thus, the efficiency decreases as the illumination wavelength departs from the nominal one.

[block-62daa70372dc7fd38b53] These fundamental limitations prevent a massive market rollout of échelette-Fresnel lenses and other diffractive optical elements in applicative areas. Indeed, imprinting the wrapped phase on a surface can be performed with structures other than the échelette. The art of flat-lens design is to devise recipes to lift the fundamental limitations of échelettes, minimizing the spurious effects introduced by the surface discontinuities. As will be shown, two generic approaches have been proposed. In the first approach, the phase results from a propagation delay (like for échelette), but implemented through an effective-index modulation and a waveguiding effect experienced by the transmitted wavefront. In the second approach, the phase is monitored by the scattering of nanoantennas with graded sizes or orientations. Both require a fine spatial sampling with subwavelength structures to faithfully imprint the rapid spatial variation of the wrapped phase at the outer zones of the lens.

## [block-02020ccd98846b6b734e] 3. High-NA metalens: an historical fresco

[block-0508d3f86f968db0fdd3] Because it combines both approaches (waveguiding and antenna emission), it is appropriate to start with the delay lens studied during the second world war by Kock [Koc48], who fabricated microwave diffractive lenses in high effective-index artificial dielectrics obtained by doping polystyrene foam sheets with subwavelength metallic insets, i.e. antennas. Drastic weight reduction was achieved with halfwavelength resonant insets, but broader frequency operation was achieved with even slightly smaller off-resonance insets. Amusingly, the delay results from both propagation and resonant scattering. The microwave domain also offers devices very similar, at least conceptually, to the metalenses that we will introduce hereafter, a particularly stringent case being the reflectarray antenna that utilizes arrays of waveguides to produce various beams [Ber63]. Extension to shorter wavelengths, did not occur until much more recently. Researchers in Erlangen [Sto91] and at MIT [Far92] were the first to propose monitoring the phase of graded-index artificial materials in the infrared, and then in the visible domain by a (relatively) slow local change of the profile of highly subwavelength gratings. The first experimental demonstrations of reflection gratings and a cylindrical mirror were performed at 𝜆 = 10.6 µm with metallic grooves of graded widths [Kip94]. Initial demonstrations of graded effective-index artificial-dielectrics manufactured for visible frequencies by etching quartz, glass and polymer films were disappointing, with measured diffraction efficiencies smaller than those of échelette components [Che95,Zho95,Mil96,Che96,War96]. At that time, the design mostly assumed an "adiabatic" effective index gradient, the analogy between local subwavelength gratings and artificial dielectrics was not properly understood, and modeling was challenging. Importantly too, the combined requirements of subwavelength spacing and 2𝜋-phase excursion led to aspect ratios difficult to manufacture with the materials and patterning technologies implemented. New perspectives opened up by manufacturing graded artificial dielectrics components in high-index materials. For operation in the visible, TiO2 appeared to be the most suitable transparent material. Figure 2 summarizes the main results in [Lal98,Lal99], which were obtained for a series of gratings with

[block-44ce2126a77858a248ad] increasing deviation angle for operation at the 𝜆 = 633 nm HeNe laser line. The smallest pillar width is

[block-cbeb75a4092a3ab928bf] 90 nm, and the pillar spacing used to sample the phase is 272-nm. The gratings, called "blazed binary" components by the authors since they introduce a new type of blazed components derived from binary masking, were fabricated by direct e-beam writing, liftoff and chemically-assisted reactive ion etching using a process detailed in [Lal98]. Remarkably, efficiencies surpassing the hegemonic échelette components were observed for highly dispersive gratings corresponding to large deviation angles. The same concept applies to lenses: for the first time, one could therefore envision implementing efficient lenses with high-NA, removing the first limitation of échelette diffractive lenses.

[block-f114d9e8075dba18c177] Indeed, that demonstration was immediately followed by the fabrication of a 20° off-axis lens operating at 𝜆 = 850 nm with a vertical-cavity-surface-emitting laser for possible application to optical interconnects, see the SEM picture shown in Fig. 3c. The lens had an NA of 0.25 but since the it was designed for a 20° off-axis operation, the Fresnel zone widths were all smaller than 9𝜆. Half of the aperture was composed of zones with widths smaller than 3𝜆, corresponding to a much larger NA for an on-axis lens. Even so, the measured efficiency, including Fresnel reflections, reaches an impressive 80% [Lal99].

[block-3d31faab7bc2f4f69f92] Fig. 2. Blazed binary gratings beat their échelette counterparts with respect to the efficiency. First-order transmission efficiencies of blazed-binary gratings as a function of the deviation angle 𝜃 in air (the grating period is varying) for a normally-incident and unpolarized plane wave at 𝜆 = 633 nm. Circles: experimental results. Red curve: theoretical results. The discrepancy at small 𝜃 is due to over etching fabrication errors resulting in a bond failure of tiny pillars. Black curve: theoretical results for échelette gratings in glass. The lower inset is an electron micrograph of a section of the grating operating at 𝜃 =20°; the period is 1.9 µm, the pillar aspect ratio reaches 4.6. The right inset shows the shadow zone responsible for efficiency drop of échelette gratings at large 𝜃's. After [Lal98,Lal99,Lee02].

[block-5eccf2202d6009733650] It is appropriate to underline why the échelette efficiencies were surpassed. For large deviation angles, the prism angle in each échelette period is as large as to cast shadow on the neighbors, wasting light into spurious orders, as illustrated in the right-hand side inset of Fig. 2 for normal incidence. The effect worsens at large deviation angle and at oblique incidence, as the relative impact of the shadow zone on the diffraction increases. In contrast, for high-index blazed-binary diffractive elements, every pillar behaves as an independent monomode waveguide that confines the light throughout the structure. No shadowing effect occurs even for oblique incidence [Lee02] with the small diffraction zones of high-NA components. In fact, such components operate at the limit of validity of the effectiveindex concept and behave analogously to phased arrays in radars [La99a]. Among other recent implementations, the most remarkable application of such components to date might be the Gaia grating [Zei10] sent to space in 2013 (period 3.15 µm, efficiency >80% over a 20.5 x 15.5 cm area).

[block-4d909a3ff38119843f36] The subject of metalenses has received considerably renewed attention recently with the introduction of resonance effects for monitoring the phase. In 2011, [Ish11] revived the interest of modulating the phase using metalic nanostructures to control the phase using the plasmonic dispersion inside a waveguiding slit in a metal and demonstrated focusing. Because resonance amplifies the phase delay compared to the propagation delay of Eq. 1, the requirement for large aspect ratios can be considerably relaxed. Indeed, the possibility to shape beams with graded resonant structures was considered in [Yu11], with plasmonic-based metasurfaces composed of tiny metallic nanoantennas. Although the research article insists on the deep-subwavelength thickness of the metasurface, see the Table in Fig. 3, the most important novelty is likely to be the demonstration that graded phases can be implemented by carefully designed nanoantennas. Following this finding, the domain has evolved quite rapidly. First it was realized that, in addition to Ohmic losses in the metal, single layer ultra-thin transmit arrays are reluctant to achieve 2𝜋-phase delays in co-polarization or impedance matching in cross polarization [Dat01,Arb14] and are thus efficient in reflection mode operation only [Hua13,Che14], and that it is advantageous, for transmission operation, to replace the nanoantennas by high-index dielectric subwavelength microresonators. Whence the recent revival of interest for dielectrics metalenses. In fact, because those components are thin, a slight absorption can be tolerated for some applications, so that silicon can be considered for use in the visible, as has been shown recently [Fat10,Vo14,Lin14,Wes14].

[block-54a051c2a0377c1d4233] Overall, two physical effects are nowadays exploited to achieve the required 2𝜋-phase excursion, either a propagation delay to implement waveguide-type metalenses [Lal99,Zie10]) or a resonance delay [Vo14,Arb15,Yu15,Dec15] to implement resonant high-contrast metalenses by combining two resonances, each covering a standard phase range of 𝜋. Additionally, each effect may be implemented with centro-symmetric nanostructures to fabricate polarization insensitive metalenses or with rotationally-asymmetric nanostructures showing form birefringence. By rotating the nanostructures, full wavefront control can be achieved with a Berry-phase vortex [Has03] and a fancy mutual polarization behavior for right-handed and left-handed input-output beams arises [Lin14, Kho16].

[block-c5edac6c9ec5c366eed3] By way of summary, Fig. 3 provides an historical overview of the concepts used so far for designing and fabricating flat optical wavefront shapers based on subwavelength binary nanostructures. One fairly exhaustive review of publications in the field appeared while this paper was being revised [Gen17].

[block-ab73ba4574614b5bb41a] Figure image region; pixels have not been interpreted.

[block-fb1179d88a983d818ebf] Fig. 3. Historical fresco of wavefront shaping with subwavelength binary nanostructures at optical frequencies. R and T mean reflection and transmission mode operation, respectively, and "cross" refers to incident and diffracted beams which are mutually cross-polarized.

[table cell block-a2ce920e8d1d5e11a5af row=0 col=0 rowspan=1 colspan=1 header=True] Reference
[table cell block-9b6c44587a3330dc8fd3 row=0 col=1 rowspan=1 colspan=1 header=True] Efficiency
[table cell block-45c0b087fd1532dc88b6 row=0 col=2 rowspan=1 colspan=1 header=True] mode
[table cell block-c4f3bc4b6f09047632ce row=0 col=3 rowspan=1 colspan=1 header=True] material
[table cell block-323e3c43be120855a422 row=0 col=4 rowspan=1 colspan=1 header=True] 𝛌
[table cell block-0a1fa4d6d2f58bd5e947 row=0 col=5 rowspan=1 colspan=1 header=True] polarization
[table cell block-498128851d84308176ad row=0 col=6 rowspan=1 colspan=1 header=True] 𝒕 𝛌 ⁄
[table cell block-6e5bf21ad746a7706a28 row=1 col=0 rowspan=1 colspan=1 header=False] a: [Koc48]
[table cell block-89d9b8ca82c75846a29a row=1 col=1 rowspan=1 colspan=1 header=False] not relevant
[table cell block-485a9494cc96a42acc7d row=1 col=2 rowspan=1 colspan=1 header=False] T
[table cell block-6ee4d147d3cd397e3ec0 row=1 col=3 rowspan=1 colspan=1 header=False] composite
[table cell block-18dee13995494ecdd5e8 row=1 col=4 rowspan=1 colspan=1 header=False] 7 cm
[table cell block-0c9b5983c543e0bf4541 row=1 col=5 rowspan=1 colspan=1 header=False] insensitive
[table cell block-d29ad2b6280fc53a0169 row=1 col=6 rowspan=1 colspan=1 header=False] not relevant
[table cell block-f243ef0e41c9bbc32d95 row=2 col=0 rowspan=1 colspan=1 header=False] b: [Kip94]
[table cell block-f9073b4c1ad976b6f8b5 row=2 col=1 rowspan=1 colspan=1 header=False] 80%
[table cell block-3939af1f2e96c02ed4fd row=2 col=2 rowspan=1 colspan=1 header=False] R
[table cell block-27a62c65ddd47834fb6c row=2 col=3 rowspan=1 colspan=1 header=False] Ag
[table cell block-a7db0b220313aa980d27 row=2 col=4 rowspan=1 colspan=1 header=False] 10.6 µm
[table cell block-43fdf96777c31a9cdf6d row=2 col=5 rowspan=1 colspan=1 header=False] linear
[table cell block-1938f6720822f0892351 row=2 col=6 rowspan=1 colspan=1 header=False] 0.7
[table cell block-32104608f87a3cc9765a row=3 col=0 rowspan=1 colspan=1 header=False] c: [Lal99]
[table cell block-bb38bc33ad9918cf0fe3 row=3 col=1 rowspan=1 colspan=1 header=False] 80%
[table cell block-3c49611335cfc8cb26b8 row=3 col=2 rowspan=1 colspan=1 header=False] T
[table cell block-71fdbdf5977b1fd599ac row=3 col=3 rowspan=1 colspan=1 header=False] TiO2
[table cell block-00d77b032f459165c72b row=3 col=4 rowspan=1 colspan=1 header=False] 633-850 nm
[table cell block-dadf9cf8b0cdcf658e82 row=3 col=5 rowspan=1 colspan=1 header=False] insensitive
[table cell block-56f1cad4f0ebd6b8ad3b row=3 col=6 rowspan=1 colspan=1 header=False] 1.15
[table cell block-e9c8b002c7a5d0a38066 row=4 col=0 rowspan=1 colspan=1 header=False] d: [Zei10]
[table cell block-0b9df9c1adfb9a9dcf0d row=4 col=1 rowspan=1 colspan=1 header=False] >80%
[table cell block-1b07a5d082ab3f74dc5e row=4 col=2 rowspan=1 colspan=1 header=False] T
[table cell block-845b8969bf6f0c4d3307 row=4 col=3 rowspan=1 colspan=1 header=False] SiO2
[table cell block-e5754845e83c0748807a row=4 col=4 rowspan=1 colspan=1 header=False] 850 nm
[table cell block-8bb64d893deed68e71b6 row=4 col=5 rowspan=1 colspan=1 header=False] insensitive
[table cell block-727ded57c984f2df16cb row=4 col=6 rowspan=1 colspan=1 header=False] 2.1
[table cell block-d8dcfd0592cafd10b14a row=5 col=0 rowspan=1 colspan=1 header=False] e: [Yu11]
[table cell block-ac09b32895feeb09e23a row=5 col=1 rowspan=1 colspan=1 header=False] weak
[table cell block-32aeb56e83a662ed4308 row=5 col=2 rowspan=1 colspan=1 header=False] T
[table cell block-f3f8da231e23582acbc9 row=5 col=3 rowspan=1 colspan=1 header=False] Au
[table cell block-ee18a6ad91186968cdc9 row=5 col=4 rowspan=1 colspan=1 header=False] 8 µm
[table cell block-0f35bda129146bbc94ca row=5 col=5 rowspan=1 colspan=1 header=False] linear (cross)
[table cell block-8aed86c720da06c7026b row=5 col=6 rowspan=1 colspan=1 header=False] 0.006
[table cell block-af87579cb41a53ad69a7 row=6 col=0 rowspan=1 colspan=1 header=False] f: [Yu15]
[table cell block-4b6826365075759aef04 row=6 col=1 rowspan=1 colspan=1 header=False] 50%
[table cell block-19ed07908b403e3c0c2f row=6 col=2 rowspan=1 colspan=1 header=False] T
[table cell block-7f628c3bb30358443e89 row=6 col=3 rowspan=1 colspan=1 header=False] Si
[table cell block-d8f6a9f9c5ae8d7f7de8 row=6 col=4 rowspan=1 colspan=1 header=False] 710 nm
[table cell block-fd84999bb4ee2e812dc2 row=6 col=5 rowspan=1 colspan=1 header=False] insensitive
[table cell block-a430e17d8606bd956e29 row=6 col=6 rowspan=1 colspan=1 header=False] 0.18
[table cell block-afec878be94b239d0733 row=7 col=0 rowspan=1 colspan=1 header=False] g: [Kho16]
[table cell block-dc28feb56c1151a7dd9c row=7 col=1 rowspan=1 colspan=1 header=False] 66-86%
[table cell block-a0c4e06337912c45b7cc row=7 col=2 rowspan=1 colspan=1 header=False] T
[table cell block-758f6b61768ed7e59b2d row=7 col=3 rowspan=1 colspan=1 header=False] TiO2
[table cell block-a1c143eeeb8fcb409373 row=7 col=4 rowspan=1 colspan=1 header=False] 405-660 nm
[table cell block-61ed6b00c2d3ba82ac7a row=7 col=5 rowspan=1 colspan=1 header=False] circular (cross)
[table cell block-6d3ab6f8faa9be28ade7 row=7 col=6 rowspan=1 colspan=1 header=False] 0.9
## [block-6c586eb4edf3f498947c] 4. Limitations and perspectives

## [block-2701a2ceec1dca23f602] 4.1 Phase control at high spatial resolution

[block-fb785ef10dabe3ba8115] The art of designing a high-NA diffractive lens relies on the capability of imprinting rapidly varying phases by exploiting subwavelength guidance, or more generally confinement, in dielectric nanostructures. The issue faces two conflicting perspectives. On one side, one would prefer to have small spacing between nanostructures (right panel in Fig. 4). Then, the phase would be finely sampled, which is important especially for achieving high efficiency. For instance, we know that the efficiency of diffractive element with a slowly-varying phase that is sampled with 4 uniformly-distributed values in the interval [0, 2𝜋] is only 81%, and that 16 values are required to reach a 99% efficiency [Swa89]. On another side, in addition to fabrication issues, there is a lower bound for the spacing. Indeed, one expects that every nanostructure be electromagnetically independent from its neighbors [La99a], so that the phase at the local control points is really implemented locally at the level of every individual nanostructure rather than over an extended area covering a few neighboring nanostructures. These requirements apply to both waveguide-type and resonant metalenses. Indeed reaching a good balance between the two conflicting perspectives requires nanostructures

[block-fa6a481018fdf35eaf6d] with a strong transversal confinement of light, otherwise the sampling points do not independently control the phase. How to implement such confinements? The whole literature on waveguide-type or resonant metalenses has consistently carried one and the same response over the years: One should use nanostructures with a high-index-contrast ratios by etching films, e.g. titanium dioxide [Lal98,Kho16] or semiconductor [War96,Fat10,Vo14,Arb15] layers with a high-index often deposited on a substrate with a lower refractive index. That guarantees that the evanescent electromagnetic tails of every nanostructure decrease rapidly and are essentially damped before reaching the neighbors. The reason is obvious for metalenses relying on propagation delays controlled by tiny waveguiding pillars, and becomes clear for resonant metalenses by considering that a Mie-like resonance of various resonator geometries, e.g. rods, disks, pillars, or even spheres, can be seen as a Fabry-Perot resonator formed in short waveguides with arbitrary semi-nanoparticles forming the terminations, see for instance the detailed discussions conducted in [Has11,Tra15].

## [block-5cb203d0459893020895] 4.2 Phase encoding with monomode and multimode operation

[block-2f373ec932157f3d488b] The principles of operation of resonant and waveguide-type metalenses are contrasted. It is important to well understand differences and similarities, together with the respective advantages and limitations.

## [block-86718176df2618ce605c] 4.2.1 Waveguide-type metalenses

[block-463c314fbc115359c607] Figure 4 summarizes the main design constraints on the spacing values for waveguide-type metalenses that offer either polarization insensitive or Berry-phase vortex operation with propagation delays. In the right panel, small spacings are considered. The phase sampling is fine, but the nanopillars are coupled electromagnetically and, due to the coupling, the supermode propagation constant 𝒌𝒛 varies with the parallel wavevector 𝒌// of the incident plane wave. The shadow zone is not completely eliminated and the efficiency is low [Lal99]. In the central panel, the spacing is adequate: The nanopillars are uncoupled, their normalized propagation constant being equal to the effective index of the isolated waveguide. Thus the imprinted phase becomes independent of 𝒌// implying that the same pillar structure can be used for a broad range of incidence angles and may effectively either focus any obliquely incident plane wave, or collimate any diverging beam. The left panel corresponds to spacings that are too large. Two supermodes are propagating and the imprinted phase that depends on both modes is difficult to accurately controlled, especially if 𝒌// varies. Safely imprinting the wrapped phase requires a monomode propagation for light in the pillars, with a pillar spacing smaller than an upper bound, called the structural cutoff in [Lal99], which guarantees that a single supermode per polarization propagates in the pillar array, all the other supermodes being evanescent.

[block-69b7d0d80dc5c708d213] Overall, Fig. 4 evidences the narrow working window for suitably selecting the nanostructure spacing - an essential parameter - and faithfully imprinting the desired phase profile. The narrow window led the present authors to choose a transparent material with the highest possible index (TiO2 in the visible) so as to maximize the transversal field confinement and to choose a spacing-to-wavelength ratio of 0.47 for the TiO2 blazed-binary elements of Fig. 3c. We note that any attempt towards the use of larger spacings, to relax the fabrication constraints for instance, systematically led to the observation of lower efficiencies, and this since Chen's first attempt [Che96].

[block-2e256569d948a01c4cf7] Fig. 4. Critical choice of the nanostructure spacing in waveguide-type metalenses. Right panel: small spacings. The phase sampling is fine but the nanopillars are coupled electromagnetically. Central panel: The nanopillars are uncoupled. Their normalized propagation constant is equal to the effective index of the isolated waveguide, and the imprinted phase is independent of 𝒌//. Left panel: large spacings. Two supermodes are propagating and the imprinted phase that depends on both modes is not accurately controlled.

## [block-941fe2a8661952524739] 4.2.2 Resonant metalenses

[block-d80f2883632d2f887bcf] The design recipes of resonant metalenses are different from those of waveguide-type metalenses, and are better understood in the language of localized 3D resonator modes. By combining two frequency-matched resonances, each covering a standard phase range of 𝜋, a 2𝜋-phase excursion is imprinted by controlling the nanoresonator dimensions [Vo14,De15,Yu15,Arb15,Zha16]. The precise physical mechanism that is exploited can be well understood from a simple model, assuming that the resonant metasurface is equivalent to an idealized subwavelength array of metaatoms composed of a pair of electric and magnetic in-plane dipoles with Lorentz polarizabilities [De15].

[block-e018681ec3c50f49014f] Two resonant geometries have been mainly considered so far. Microposts offer high efficiencies. However, they can be used only for a reduced range of duty cycles to avoid spurious resonance with nearly null transmittance [Arb15,Zha16,Vo14], and the aspect ratio is only weakly relaxed in comparison with the monomode guidance approach. Nanodisks [De15,Yu15] appear more promising as they allow for significantly smaller aspect ratios that are fully compatible with standard industrial silicon technology, see Fig. 3f. Although the measured efficiencies to date are smaller than 60%, ultra high performance with nearly-100% transmission efficiency appears realistically achievable with Si nanodisks embedded into an optimized dielectric environment, as predicted by computational results [De15].

[block-d6c86d325cbe42628179] If two localized symmetric modes coexist for a given wavelength, an antisymmetric mode must also exist at the same wavelength. This assertion can be intuitively understood by considering the equivalence between Mie-like localized resonances and Fabry-Perot resonances [Tra15]. Since the two localized symmetric modes are necessarily formed by the cycling of two symmetric guided modes, we know from waveguide theory, that an antisymmetric guided modes should also exist at the same wavelength and it is thus expected that another localized mode, formed by the cycling of the antisymmetric guided mode, exist.

[block-8835cbe6a6046ea86e43] To evidence the existence of this third localized mode and to study its impact, we consider the stateof-the-art Huygens' metasurface proposed in [Dec15] and compute its transmittances at normal and small oblique incidences. The transmittances are displayed in Fig. 5. At normal incidence (black curve), consistently with the red curve in Fig. 5c in [Dec15], nearly 100% transmission efficiency is impressively observed over a 2𝜋-phase excursion (we have not plotted the phase-variation but the interested reader may refer to the blue curve in Fig. 5c in [Dec15]). However, the transmittance spectra computed at slightly oblique incidences (blue and dotted-red curves) display dips that are typically due to the excitation of antisymmetric resonances not excited at normal incidence. These dips, which in addition are strongly polarization dependent, considerably reduce the efficiency. A different way to understand their existence can be well grasped from the physics of resonant high-contrast gratings with symmetric and antisymmetric Bloch modes, as discussed in detail in [Lal06].

[block-0e6b0590d4ea37afd4c0] To our knowledge, the detrimental impact of antisymmetric localized modes has not been discussed in the literature of resonant metalenses [Vo14,De15,Yu15,Arb15,Zha16]. It may explain why, despite the ease of fabrication of low-aspect-ratio nanodisks, the measured efficiency, inevitably based on imperfectly collimated and slightly tilted incident beams, is significantly lower than the efficiency computed for normal incidence. Antisymmetric localized modes may also severely limit the applicability of resonant metalenses, if they are not taken into account in the design.

[block-73841519e52cc309f7cf] Fig. 5. Theoretical transmittance spectrum of the Huygens' metasurface composed of a 2D array of Si-disks. The geometrical and material data are taken from [Dec15]: the disk diameter is 484 nm, the disk thickness is 220 nm, the x-and y-periods are 668 nm, and the 2D array is assumed to be buried in material with a refractive index 1.66. The transmission efficiency for normal incidence (black curve) is nearly 100%, in quantitative agreement with the computational results shown in Fig. 5c in [Dec15]. The blue curve and red dotted curves (not computed in [Dec15]) are obtained for slightly oblique incidences. TE and TM polarizations refer to an incident plane wave (red arrow

[block-2d6d19163273448b1584] in the inset) with an electric field parallel and perpendicular to the (z,y) plane. The computations are performed with a Fourier Modal Method [Pop00].

## [block-eb822c24e69330d98344] 4.3 High-NA high-efficiency metalenses

## [block-c5da25644a19ab279438] 4.3.1 Efficiency

[block-ea8ff47a4d4f4ebed469] Large efficiencies, defined as the ratio between the power funneled into a specific desired order and the incident energy impinging on the diffractive element, are required for most applications. It is this efficiency that was considered in the previous figures and tables. It includes reflection losses. For imaging applications, it is acceptable that some power is lost by reflection/absorption, and another figure of merit, the relative efficiency defined as the fraction of the transmitted power that is effectively diffracted into the desired diffraction order, may be the proper parameter to maximize. Indeed, light that is not scattered into the desired diffraction orders feeds spurious diffractions orders, a set of diverging or converging spherical waves with focal lengths that are integer sub-multiples of the nominal focal length. They deteriorate image quality by effectively superimposing a coherent background.

## [block-31339bd1ab74a04c6212] 4.3.2 State of the art

[block-a29b274fcc032dbb2951] In the perspective of accessing the present status of the field of high-NA high-efficiency metalenses, it is appropriate to analyze the recent flagship publication [Kho16] reporting metalenses respectively designed for operation at blue, green and red wavelengths with a huge NA of 0.8. Table 1 puts into perspective the achievements in [Kho16] in comparison to previous state-of-the-art results [Lal99]. One should note here that [Arb15] has reported an even higher NA (0.88) in a polarization insensitive metalens used in the telecommunication infrared range.

[block-5ea1e38b24951d1ca542] Except for the phase encoding approaches, the works in [Kho16] and [Lal99] present many similarities, since both considers waveguide-type metalenses, nanostructures formed with TiO2 pillars with similar aspect ratios and almost identical spacing-to-wavelength ratios especially for the "blue" lens. There is no apparent real conceptual rupture. However, with a 86% efficiency, the blue lens is quite remarkable. It would be interesting to know what is the effective efficiency, but it could be reasonably expected that at least 90% of the transmitted power is effectively diffracted into the desired diffraction order, a remarkable value in view of the high NA that is achieved.

[block-171f3628940198dc4997] It is interesting to understand why the blue metalens has a slightly better diffraction efficiency than the blazed-binary diffractive elements. One possibility is a better fabrication technology. Another hypothesis is that polarization independence requires that the phase is varied by changing the size of the pillars. In total not all pillars are really independent from each other, especially tiny ones. In contrast, all pillars of the metalens are identical up to a rotation, and independence might well be better satisfied.

[block-d32f51e162c434ab1772] Also the 86% efficiency represents an averaged value over the entire lens, and it would be interesting to appreciate what is the actual efficiency achieved at the outer zones of the lenses, which indeed is not easy. The focal spot FWHM, as reported in [Kho16], is a good indicator of diffraction limited operation only when the focal spot matches an Airy pattern. But the focal spot sidelobes are enhanced compared to an Airy pattern, an observation that is consistent with the observation of a departure of the MTF (Fig. 2 of [Kho16]) from that of a diffraction limited lens. In other words, the characterization of metalenses is an emerging problem that [Kho16] has started addressing, but that will require more work in the metalens community.

[block-a5fb0a8c2d74e7c88839] Figure image region; pixels have not been interpreted.

[block-0a79c07834e782eda11b] Blazed binary elements [Lal99]

[block-68619b5bfa1dc3e49f07] Metalenses [Kho16]

[block-d04daeff6fb8a00bdefa] Material

[block-c139dbea965d40cb503c] TiO2

[block-60b43077c07a839a89bf] TiO2

[block-6d2dcbbf3261825c14b8] Wavelength (nm)

[block-63c65858823ad050d8f8] Spacing 𝑆/𝜆

[block-78e895d2cb4f3fe00481] Efficiency

[block-3b08a7f0a819bd57173c] 633 - 860

[block-8cd1d8c16bcd64e9f346] 0.47

[block-ed5337b7f69540347ed7] 80% (absolute)

[block-b2315bc95e161d990a2f] 405 (blue)

[block-00b9d74da08395876e76] 0.49

[block-adea0076bac3809764f4] 86%

[block-15ece999b8db76e2d618] 532 (green)

[block-f5f46990d123608f9fc0] 0.61

[block-1aade151d52055db09d0] 73%

[block-5277b571520658787791] 660 (red)

[block-aeda8df1af6361395d3d] 0.65

[block-46f7a88c18b2acc1d3e6] 66%

[block-892070d89c9a33308eeb] Polarization

[block-7d95831e019fa5a69ddc] Insensitive

[block-09401389f8b098f00c94] Circular (cross)

[block-c701b18e9a5996747977] NA

[block-6fe3ec9f30379177a9c1] 0.64

[block-bfc9fbb1ed8c6adf63e9] 0.8

[block-9642d456cf4ed46a90bd] Table 1. Comparison between the blazed-binary gratings and lens manufactured in [Lal99]and the 2016 metalenses in [Kho16]. The phase encoding technique used for the metalens operates with circularly polarized light, whereas the technique in [Lal99] is polarization insensitive. Absolute efficiencies are considered in [Lal99], whereas in [Kho16] the efficiency relates a beam that has already experienced a ~4% reflection loss.

[table cell block-8bfdff533b56e70433a9 row=0 col=0 rowspan=1 colspan=1 header=False] Blazed binary elements [Lal99]
[table cell block-99902c2eb8d6fc34275b row=0 col=1 rowspan=1 colspan=3 header=False] Metalenses [Kho16]
[table cell block-d62fb3257420ef628afc row=1 col=0 rowspan=1 colspan=1 header=False] Material
[table cell block-aff41acc522bf810d570 row=1 col=1 rowspan=1 colspan=1 header=True] TiO2
[table cell block-88b1e5506d5093c6aa4a row=1 col=2 rowspan=1 colspan=3 header=True] TiO2
[table cell block-06fac297ad95cd501785 row=2 col=0 rowspan=1 colspan=1 header=False] Wavelength (nm)
[table cell block-68e986bde8d5e106a452 row=2 col=1 rowspan=1 colspan=1 header=False] 633 - 860
[table cell block-a923d5daf3eee4d51b53 row=2 col=2 rowspan=1 colspan=1 header=False] 405 (blue)
[table cell block-9d8eca8a973fc80eefc9 row=2 col=3 rowspan=1 colspan=1 header=False] 532 (green)
[table cell block-3c3eece7cfefbc7b97de row=2 col=4 rowspan=1 colspan=1 header=False] 660 (red)
[table cell block-bd2b9b6a4a83ef74f649 row=3 col=0 rowspan=1 colspan=1 header=False] Spacing 𝑆/𝜆
[table cell block-4e8ad07ce3d274cf31c0 row=3 col=1 rowspan=1 colspan=1 header=False] 0.47
[table cell block-5fbf7ea8c00d754cb0f8 row=3 col=2 rowspan=1 colspan=1 header=False] 0.49
[table cell block-4d5a02d0d3f99d6c29f0 row=3 col=3 rowspan=1 colspan=1 header=False] 0.61
[table cell block-f69c198053921a2fab42 row=3 col=4 rowspan=1 colspan=1 header=False] 0.65
[table cell block-4ee62f0349a96f7aeebf row=4 col=0 rowspan=1 colspan=1 header=False] Efficiency
[table cell block-7b27accd5fe5bd2b4cec row=4 col=1 rowspan=1 colspan=1 header=False] 80% (absolute)
[table cell block-b0479d8992cbe082d6c1 row=4 col=2 rowspan=1 colspan=1 header=False] 86%
[table cell block-935236dee3ece10dac28 row=4 col=3 rowspan=1 colspan=1 header=False] 73%
[table cell block-570a12a7b00f1af1cc9a row=4 col=4 rowspan=1 colspan=1 header=False] 66%
[table cell block-d2d71931e5b3f39bcc20 row=5 col=0 rowspan=1 colspan=1 header=False] Polarization
[table cell block-a49a0824576c04dec607 row=5 col=1 rowspan=1 colspan=1 header=False] Insensitive
[table cell block-efbe0b07becf86752b11 row=5 col=2 rowspan=1 colspan=3 header=False] Circular (cross)
[table cell block-6394e1e9263009cbe1a4 row=6 col=0 rowspan=1 colspan=1 header=False] NA
[table cell block-7d2727bd48c2334b8109 row=6 col=1 rowspan=1 colspan=1 header=False] 0.64
[table cell block-3e603128d1d9cf10d63d row=6 col=2 rowspan=1 colspan=3 header=False] 0.8
[block-f7863894c7eedd4db93c] Achieving high efficiency at high NA or deviation is really challenging. The width w of the outer zones of a lens is actually 1/NA when normalizing to the wavelength, implying that for NA=0.8 only 2.5 (resp. 2.0) pillars on average are used to sample the phase of the outer zone of the blue (resp. green) metalenses. In [Lal99,Lee02], substantial computations and optimizations were performed to evaluate what could be the best diffraction efficiencies achievable with high-NA blazed-binary lenses fabricated with TiO2 pillars by studying gratings with the same period as the zone widths. Essentially, it was found that the relative efficiency slowly decreases from 100% for low NA to 85% for NA= 0.5. For larger NA's, the zone width becomes comparable to the wavelength and resonance effects occur, producing chaotic unintuitive variations of the efficiency. Nevertheless, it was possible to design gratings with a deviation angle 𝜃 = 51° (NA = 0.78) with a relative efficiency of 83% and to demonstrate experimentally a blazedbinary grating with a deviation 𝜃 = 40° (NA = 0.64) and an efficiency of 80%. Further simulations seem to indicate that, due to fabrication errors of the precise dimensions of the nanopillars and residual roughness, the measured efficiencies are 5-7% smaller than the theoretical values and ≈ 5% smaller than the relative efficiencies.

[block-267836fc7cdfd94e0cf6] These results suggest that it would be very instructive to redesign a series of gratings with varying periods for the technology used in [Kho16], to fabricate and measure their efficiencies in order to quantitatively predict the efficiencies of the outer zones of metalenses fabricated with the technology in [Kho16]. That is essential to in-fine predict the image quality of high-NA metalenses.

## [block-c5a85ec147d9006686cf] 4.4 Field of view

[block-839b25051e0f2e2e6ad2] Commercial lenses for imaging applications are composed of several elements made from diverse materials, and some of their surfaces exhibit non-spherical shapes. Spatial separation of the elements along the lens axis is required to correct for aberrations when imaging at wide angles and large fields with diffraction-limited focusing spots. In sharp contrast, all metalenses fabricated up to now using the various approaches mentioned above use a single element to imprint the phase. Whichever the method used for encoding the phase, that precludes high quality imaging over a large angular extent, as we shall illustrate now.

[block-c18ee32561df8423330c] The issue can be illustrated by considering Fig. 6, which shows focusing at normal incidence (dashed blue) and at oblique incidence (solid red). Denoting by 𝑥 and 𝑦 the pupil coordinates, elementary geometrical calculations show that the phase-difference between points A and B that should be implemented for perfect focusing at normal incidence is (φA -φB) = 𝑘0(𝑓 2 +𝑥 2 +𝑦2 )1/2 -𝑘0𝑓, whereas it should be 𝑘0 (𝑓 2 + (𝑥 -𝑓𝑡𝑎𝑛(𝜃))2 +𝑦2)1/2 -𝑥𝑠𝑖𝑛(𝜃) at oblique incidence 𝜃. Here, B is an arbitrary point in the pupil, that can move up to the border as shown in the figure. The departure between these two expressions can easily be expanded, for a small pupil size 𝑟 and a small angle 𝜃, into the standard aberrations of coma, field curvature, and astigmatism. As said, those are normally compensated in a compound lens by taking benefit of the distance between the lens elements. In a single lens, those aberrations increase quite rapidly with increasing aperture or field angle, and there is not much that can be done about it.

[block-fef079dc219172a584af] Therefore, for a large NA, the field of view is very small. For example, for NA = 0.5, with a focal length of 1 mm, the low aberration field of view with a path delay error not exceeding one wavelength, in the visible, is restricted to less than 5 µm. This implies that single-interface lenses might be useful for pointto-point interconnects but not for high-performance imaging. Having that consideration in mind, it is inappropriate and even misleading to compare the imaging performance of a single lens and multi-lens objectives corrected for aberrations, as done in [Kho16].

[block-c84e98844abf9d2d58a8] Fig. 6. Geometrical aberrations with single-surface imaging.

## [block-2bcb70993e6fc89294b8] 4.5 Broadband operation

[block-fe7a1373ebfb5d59b9b7] The 2𝜋-phase excursion is in general achieved at a single wavelength, the blaze wavelength used for the design. Illuminated at another wavelength, the 2𝜋-phase excursion is not maintained and light is no longer funneled into a single order; the deviation results in the apparition of deterministic scattering into spurious diffraction orders, which represents a severe limitation for optical imaging systems designed to operate over finite spectral bands. Because the dispersion of most materials is weak away from their absorption bands, the efficiency dependence with the wavelength obeys a universal law 𝜂 = sinc2(1-𝜆0 𝜆 / ), 𝜆0 being the blazed wavelength for which the 2𝜋-phase excursion is achieved [Swa89].

[block-c949c1daa49647dbb034] Metalenses bring a new degree of freedom. Because the artificial dielectric is much more dispersive than a bulk material, one should consider the dependence of the effective index with the wavelength, and the optical phase difference is now written

> [raw formula asset asset-4ea43d802b3a451c32fb; representation=xml]
> ```text
<disp-formula id="d00191" data-docling-location="{&quot;bbox&quot;:[72.144,165.00487999999996,539.9958399999999,184.7217227789473],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;docling_primary&quot;:true,&quot;docling_prov&quot;:[{&quot;bbox&quot;:{&quot;b&quot;:607.2782772210527,&quot;coord_origin&quot;:&quot;BOTTOMLEFT&quot;,&quot;l&quot;:72.144,&quot;r&quot;:539.9958399999999,&quot;t&quot;:626.99512},&quot;charspan&quot;:[0,29],&quot;page_no&quot;:13}],&quot;kind&quot;:&quot;formula&quot;,&quot;locations&quot;:[{&quot;bbox&quot;:[72.144,165.00487999999996,539.9958399999999,184.7217227789473],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;charspan&quot;:[0,29],&quot;page&quot;:13,&quot;page_height&quot;:792.0,&quot;page_rotation&quot;:0,&quot;page_width&quot;:612.0,&quot;unit&quot;:&quot;pt&quot;}],&quot;order_index&quot;:190,&quot;owner_region_id&quot;:&quot;&quot;,&quot;page&quot;:13,&quot;parser&quot;:&quot;docling&quot;,&quot;parser_config_sha256&quot;:&quot;ed0fec77c24013ad15529fe5c33c3cc37d88043be981defbca378f4ecd0def23&quot;,&quot;parser_version&quot;:&quot;2.138.0&quot;,&quot;pdf_sha256&quot;:&quot;e3e00b4db932b9f95225150b11685dea6276fbc27f9f24408bd54679f5456ec1&quot;,&quot;projection_version&quot;:&quot;optomind.docling_body.v1&quot;,&quot;recognition_status&quot;:&quot;missing&quot;,&quot;region_id&quot;:&quot;d00191&quot;,&quot;representation&quot;:&quot;unenhanced_pdf_text&quot;,&quot;source_json_pointer&quot;:&quot;/texts/98&quot;,&quot;source_ref&quot;:&quot;#/texts/98&quot;,&quot;text_sha256&quot;:&quot;48f7f28ea381f5890425ad67c115d4d2cf9a653027701a1f5d666cc8030c4e3c&quot;}" data-recognition-status="missing"><p>Unrecognized formula region; parser_text: 2𝜋 𝜆 Δ = 2𝜋 𝜆 (𝑛(𝜆) -1)𝑡. [3]</p></disp-formula>
> ```

[block-64d2e3aa1e904d3b03e9] With this extra degree of freedom, it is possible to exploit the highly chromatic behavior of semiconductor structures with dimensions only slightly smaller than the wavelength to make the product 2𝜋 𝜆 (𝑛(𝜆) -1) nearly independent of wavelength. The idea initiated in the context of broadband subwavelength phase-plate gratings [Kik97] was tested in [Rib13] for metalenses composed of a careful combination of micropillars and microholes. The device has been characterized in the thermal infrared (band III) and blazing over nearly one octave was observed experimentally.

[block-5ac368a8e1150ad145c9] Not only does the efficiency of a diffractive lens varies with the wavelength, but so does its focal length, 𝑓(𝜆2)𝜆2 = 𝑓(𝜆1)𝜆1. Actually, the focal length decreases with wavelength, in sharp contrast with refractive lenses based on various glasses for which the refractive index (resp. focal length) decreases (resp. increases) with wavelength. It is this complementarity that is exploited in achromatic hybrid doublets to reduce the weight and size of optical systems [Blo95].

[block-c88c0e71246bb3ec6170] Instead of using the complementary strengths of refractive and diffractive lenses in hybrids, in the spirit of completely replacing refractive lenses by diffractive ones, one may envision to manufacture lenses with focal lengths that are independent of the wavelength [Kho15]. That cannot be performed over a continuum of wavelengths, but it can be envisioned for a discrete set of wavelengths, at blue, green and red wavelengths (RGB) for instance. In Fig. 7, we sketched the pressure placed upon the designer to imagine a multiplexed metalens that imprints three graded phases independently at three different wavelengths. Starting from a classical graded metalens in (a), each nanostructure should be replaced by three even-smaller colored nanostructures with effective cross sections that are more than 10 times larger than their physical cross sections, so that all the light incident on every unit cell (dashedline box in (a) or (b)) is funneled into the nanostructure to imprint the desired phase at the nominal wavelength. In the spectral domain, see (c), each colored nanostructure should be wavelength-selective to avoid inevitable spectral crosstalk due to the spurious scattering by neighbor nanostructures designed for operation at a given wavelength and illuminated at another. All in all, fulfilling that dual requirement for every nanostructure represents a real challenge with the design of deep subwavelength nanoresonators that efficiently funnel light in the spatial domain and provide high Q resonance in the spectral domain. Even if the requirements are physically linked by space-bandwidth products arguments, high efficiency and faithful operation will be challenging to reach.

[block-20346fde918e45644907] Fig. 7. Big design challenge for metalens operating independently at three different wavelengths. To avoid crosstalk, the multiplexing should fulfill drastic requirements in space (antenna funneling) and spectral (high Q) domains.

## [block-d3d2a32163173ef9923c] 5. Conclusion

[block-8d56be633cb1e0811e20] The previous analysis leads us to the following conclusive points:

[block-456e0d95df5a3a27ccea]  Focusing light with high-efficiency and high-NA is feasible at one wavelength in a point-to-point interconnection scheme with a single interface patterned with subwavelength high-index structures that guide light. This property, which cannot be achieved with classical échelette diffractive optical elements, has been known since the late 90's [Lal99, La99a].

[block-9fadf367e1ef31e6381a]  High performance imaging (with high-NA) is not possible with a single diffractive (or refractive) interface, see Section 4.3. Manufacturing high-efficiency and high-NA metalenses for highperformance imaging requires to correct geometrical aberrations with several diffractive surfaces, as recently demonstrated in [Arb16]. It would be interesting to know what could be the minimum separation distance between the surfaces, but thinness of the global optical system and high performance imaging over a wide field of view seem to be two incompatible requirements [Gis16].

[block-25934676b4a68b33876f]  Broadband blazing (typically over one octave) with high relative efficiency has been demonstrated for applications in the thermal infrared with metasurfaces that exploit the highly chromatic behavior of subwavelength structures, see Section 4.4. Unfortunately the demonstration uses holes, instead of pillars, in the shadow zone and since holes do not guide light efficiently, high efficiency is achieved only for devices with low deviation angles 𝜃. At this stage, there is no known metasurface design that offers broadband blazing and high NA. Note that multilayer diffractive échelette-type components exist for broadband operation with ultra-high efficiency (> 95%) and low-NA [Nak02]. Such components have been pioneered by Cannon in the EF 400mm f/4 DO IS USM teleobjective lens.

[block-87b9378c832e41cf43a1]  Manufacturing cost matters. Echelette diffractive optical elements are presently manufactured at very low-cost in plastics essentially, with replication techniques such as roll-on, embossing … [Gal97]. There is a common underlying cost to manufacturing metalenses. The latter require subwavelength features with large aspect ratios, see the last column in the Table of Fig. 3, making mass production a real challenge. In this line of thought, fabrication constraints appear relaxed with metalenses implementing 2𝜋-phase excursion with a combination of two resonances rather than with a propagation delay. The fabrication cost of metalenses based on plasmonic nanoantennas that may potentially be patterned on a low-cost flexible substrate with roll-on techniques could be markedly lower. However, in addition to efficiency issues, the low defect requirement needed to lower inevitable scattering due to roughness and impurities seems challenging as well.

[block-50b338a2eda06129c143] Aside from those concerns on imaging issues with metalenses, niche applications may exist. Metasurfaces that provide fancy polarization behaviours with birefringent nanopillars, which cannot be easily implemented with classical refractive or échelette components, may offer new opportunities as recently demonstrated for single molecule spectroscopy for instance [Bac16].

## [block-72c72454df37452f709c] Acknowledgements

[block-abd69c48e5c377978a82] The authors have appreciated the repeated, fruitful and friendly discussions with Jacob Khurgin, Yuri Kivshar, Erez Hasman, Sergei Bozhevolnyi, Vlad Shalaev, Arka Majumdar, Thomas Krauss, Pavel Cheben, Andrei Faraon, Arseniy Kuznetsov, who helped much refining our opinion on this booming field. They also wish to thank five anonymous reviewers for their kind interaction, repeated questioning and persuasive comments.

## [block-e82818e68d0bb028592c] References

[block-8d70edbcb379d74e90de] [Zhu16] A.Y. Zhu, A.I. Kuznetsov, B. Luk'yanchuk, N. Engheta, and P. Genevet, Traditional and emerging materials for optical Metasurfaces, Nanophotonics 6, 452-471 (2017).

[block-21338bf2ec16704fa2ff] [Kuz16] A.I. Kuznetsov, A. E. Miroshnichenko, M. L. Brongersma, Y. S. Kivshar, B. Luk'yanchuk, Optically resonant dielectric nanostructures, Science 354, aag2472 (2016).

[block-52528ff8540850bf2d7d] [Yu11] N. Yu, P. Genevet, M. A. Kats, F. Aieta, J.-P. Tetienne, F. Capasso, Z. Gaburro, Light propagation with phase discontinuities: generalized laws of reflection and refraction, Science 334, 333-337 (2011).

[block-33c3fe762459c7d6da0c] [Kho16] M. Khorasaninejad, W. T. Chen, R. C. Devlin, J. Oh, A. Y. Zhu, F. Capasso, Metalenses at visible wavelengths: diffraction-limited focusing and subwavelength resolution imaging, Science 352, 11901194 (2016).

[block-311f730be7b416ae6695] [Swa89] G. J. Swanson, Binary optics technology: the theory and design of multilevel diffractive optical elements, MIT Technical Report 854 (MIT, 1989).

[block-6226f3697939e4100d7e] [Aur72] L. d'Auria, J.-P. Huignard, A. M. Roy, E. Spitz, Photolithographic fabrication of thin film lenses, Opt. Commun. 5, 232-235 (1972).

[block-aadcf6b6ed2821e01afe] [Gal97] M.T. Gale, Replication techniques for diffractive optical elements, Microelectr. Eng. 34, 321-339 (1997).

[block-feb73026395d2a5e1fea] [Koc48] W.E. Kock, Metallic delay lenses, Bell Syst. Tech. J. 27, 58-82 (1948).

[block-22b35cfa425acfa022cd] [Ber63] D. G. Berry, R.G. Malech, and W.A. Kennedy. "The reflectarray antenna." IEEE Transactions on Antennas and Propagation, AP-11, 645-651 (1963).

[block-6472d697dceb6dab59d7] [Sto91] W. Stork, N. Streibl, H. Haidner, P. Kipfer, Artificial distributed-index media fabricated by zeroorder gratings, Opt. Lett. 16, 1921-1923 (1991).

[block-8cd442c6518343588fe6] [Far92] W. M. Farn, Binary gratings with increased efficiency, Appl. Opt. 31, 4453-4458 (1992).

[block-3c2964ccf4f0bd36ed40] [Kip94] P. Kipfer, M. Collischon, H. Haidner, J.T. Sheridan, J. Schwider, N. Streibl, J. Lindolf, Infrared optical components based on a microrelief structure, Opt. Eng. 33, 79-84 (1994).

[block-34d9337ae92e8c4d773c] [Che95] F.T. Chen and H.G. Craighhead, Diffractive phase elements on two-dimensional artificial dielectrics. Opt. Lett. 20, 121-123 (1995).

[block-613f694e3ce11127a7d8] [Zho95] Z. Zhou and T. J. Drabik, Optimized binary, phase-only, diffractive optical element with subwavelength features for 1.55 µm, J. Opt. Soc. Am. A 12, 1104-1112 (1995).

[block-e52651ac0f15fbc21a04] [Mil96] J. M. Miller, N. de Beaucoudrey, P. Chavel, E. Cambril and H. Launois, Synthesis of Subwavelength Pulse Width Modulated Diffractive Optical Elements, Opt. Lett. 21, 1399-1401 (1996).

[block-ea1a47915d8bd9cd9cac] [Che96] F.T. Chen and H.G. Craighead, Diffractive lens fabricated with mostly zeroth-order gratings, Opt. Lett. 21, 177-179 (1996).

[block-23c337773ec8e2c98afe] [War96] M.E. Warren, R.E. Smith, G.A. Vawter and J.R. Wendt, High-efficiency subwavelength diffractive optical element in GaAs for 975 nm, Opt. Lett. 20, 1441-1443 (1996).

[block-a81fe1aaa4e3d409530e] [Lal98] P. Lalanne, S. Astilean, P. Chavel, E. Cambril, H. Launois, Blazed-binary subwavelength gratings with efficiencies larger than those of conventional échelette gratings, Opt. Lett. 23, 1081-1083 (1998).

[block-63d9c753dfd926fe5b61] [Lal99] P. Lalanne, S. Astilean , P. Chavel, E. Cambril, H. Launois, Design and fabrication of blazed-binary diffractive elements with sampling periods smaller than the structural cutoff, J. Opt. Soc. Am. A 16,

[block-add0d9a1c2ea284dff52] 1143-1156 (1999). [Lee02] M.S.L. Lee, P. Lalanne, J.C. Rodier, P. Chavel, E. Cambril, Y. Chen, Imaging with blazed binary diffractive elements, J. Opt. A. 4, S119-S122 (2002). [La99a] P. Lalanne, Waveguiding in blazed binary diffractive elements, J. Opt. Soc. Am. A 16, 2517-2520 (1999). [Zei10] U.D. Zeitner, M. Oliva, F. Fuchs, D. Michaelis, T. Benkenstein, T. Harzendorf, E.B. Kley, High performance diffraction gratings made by e-beam lithography, Appl. Phys. A - Mater. Sc. Eng. 109, 789796 (2012). [Ish11] S. Ishii, A.V. Kildishev, V.M. Shalaev, K.P. Chen, V.P. Drachev, Metal nanoslit lenses with polarization-selective design, Opt. Lett. 36, 451-453 (2011). [Dat01] S. Datthanasombat, A. Prata, L. R. Arnaro, J. A. Harrell, S. Spitz and J. Perret, "Layered lens antennas," IEEE Antennas and Propagation Society International Symposium. Digest 2, 777-780 (2001) [Arb14] A. Arbabi and A. Faraon, Fundamental limits of ultrathin metasurfaces, arXiv preprint arXiv:1411.2537 (2014). [Hua13] L. Huang, X. Chen, B. Bai, Q. Tan, G. Jin, T. Zentgraf, and S. Zhang, Helicity dependent directional surface plasmon polariton excitation using a metasurface with interfacial phase discontinuity, Light Sci. Appl. 2, e70 (2013). [Che14] W.T. Chen, K.Y. Yang, C.M. Wang, Y.W. Huang, G. Sun, I.D. Chiang, C.Y. Liao, W.L. Hsu, H.T. Lin, S.L. Sun, L. Zhou, A.Q. Liau, D.P. Tsai, High-Efficiency Broadband Anomalous Reflection by Gradient Meta-Surfaces, Nano Lett. 14, 225-230 (2014). [Lin14] D. Lin, P. Fan, E. Hasman, M.L. Brongersma, Dielectric gradient metasurface optical elements, Science 345, 298-302 (2014). [Vo14] S. Vo, D. Fattal, W.V. Sorin, Z. Peng, T. Tran, M. Fiorentino, R.G. Beausoleil, Sub-wavelength grating lenses with a twist, IEEE Photon. Technol. Lett. 26, 1375-78 (2014). [Fat10] D. Fattal, J.J. Li, Z. Peng, M. Fiorentino, R.G. Beausoleil, Flat dielectric grating reflectors with focusing abilities, Nat. Photon. 4, 466-470 (2010). [Has11] S.B. Hasan, R. Filter, A. Ahmed, R. Vogelgesang, R. Gordon, C. Rockstuhl, and F. Lederer, Relating localized nanoparticle resonances to an associated antenna problem, Phys. Rev. B 84, 195405 (2011) [Tra15] D.J. Traviss, M.K. Schmidt, J. Aizpurua, and O.L. Muskens, Antenna resonances in low aspect ratio semiconductor nanowires, Opt. Express 23, 22771-22787 (2015) [Pop00] E. Popov and M. Nevière, Grating theory: new equations in Fourier space leading to fast converging results for TM polarization, J. Opt. Soc. Am. A 17, 1773-84 (2000) [Arb15] A. Arbabi, Y. Horie, A.J. Ball, M. Bagheri, A. Faraon, Subwavelength-thick lenses with high numerical apertures and large efficiency based on high-contrast transmitarrays, Nature Commun. 6, 7069 (2015). [Yu15] Y.F. Yu, A.Y. Zhu, R. Paniagua-Dominguez, Y.H. Fu, B. Luk'yanchuk, and A.I. Kuznetsov, Hightransmission dielectric metasurface with 2π phase control at visible wavelengths, Laser Photonics Rev. 9, 412-418 (2015). [Dec15] M. Decker , I. Staude , M. Falkner , J. Dominguez , D.N. Neshev , I. Brener , T. Pertsch , and Y.S. Kivshar, High-Efficiency Dielectric Huygens' Surfaces, Adv. Opt. Mat. 3, 813-820 (2015). [Has03] E. Hasman, V. Kleiner, G. Biener, and A. Niv, Polarization dependent focusing lens by use of quantized Pancharatnam-Berry phase diffractive optics, Appl. Phys. Lett. 82, 328 (2003) [Gen17] P. Genevet, F. Capasso, F. Aieta, M. Khorasaninejad, R. Devlin, "Recent advances in planar optics: from plasmonic to dielectric metasurfaces", Optica 4, 139-152 (2017). [Zha16] A. Zhan, S. Colburn, R. Trivedi, T.K. Fryett, C.M. Dodson, and A. Majumdar, Low-Contrast

[block-aa53251221d90ae8776c] Dielectric Metasurface Optics, ACS Photonics 3, 209-214 (2016).

[block-c500204ce84420b16add] [Lal06] P. Lalanne, J.P. Hugonin, P. Chavel, Optical properties of deep lamellar gratings: a coupled Bloch-

[block-38d665cfa5e8a26904b8] mode insight, IEEE J. Lightwave Technol. 24, 2442- 2449 (2006). [Has12] C.J. Chang-Hasnain and W. Yang, High contrast grating for integrated optoelectronics, Advances in Optics and Photonics 4, 379-440 (2012). [Rib13] C. Ribot, M.S.L. Lee, S. Collin, S. Bansropun, P. Plouhinec, D. Thenot, S. Cassette, B. Loiseaux, P. Lalanne, Broadband and efficient diffraction , Adv. Opt. Mat. 1, 489-493 (2013). [Kik97] H. Kikuta, Y. Ohira and K. Iwata, "Achromatic quarter-wave plates using the dispersion of form birefringence", Appl. Opt. 36, 1566-72 (1997). [Blo95] C. G. Blough and G. M. Morris, Hybrid lenses offer high performance at low cost, Laser Focus World, November 1995. [Kho15] M. Khorasaninejad, F. Aieta, P. Kanhaiya, M.A. Kats, P. Genevet, D. Rousso, and Federico Capasso, Achromatic Metasurface Lens at Telecommunication Wavelengths, Nano Lett. 15, 5358-62 (2015). [Arb16] A. Arbabi, E. Arbabi, S. M. Kamali, Y. Horie, S. Han and A. Faraon, Miniature optical planar camera based on a wide-angle metasurface doublet corrected for monochromatic aberrations, Nature Comm. 7, 13682 (2016). [Gis16] T. Gissibl, S. Thiele, A. Herkommer and H. Giessen, Two-photon direct laser writing of ultracompact multi-lens objectives, Nature Photonics 10, 554-560 (2016). [Nak02] T. Nakai and H. Ogawa, Research on multi-layer diffractive optical elements and their application to camera lenses, in proceedings of the OSA Topical Meeting on Diffractive Optics and MicroOptics, Tucson, Arizona (2002). [Bac16] M.P. Backlund, A. Arbabi, P.N. Petrov, E. Arbabi, S. Saurabh, A. Faraon and W.E. Moerner,

[block-727a2edd3b300d429d36] Removing orientation-induced localization biases in single-molecule microscopy using a broadband metasurface mask, Nature Photonics 10, 459-462 (2016).
