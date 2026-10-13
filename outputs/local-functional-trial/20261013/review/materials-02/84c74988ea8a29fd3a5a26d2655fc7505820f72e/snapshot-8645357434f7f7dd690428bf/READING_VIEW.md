# Reading View

<!-- snapshot_id: snapshot-8645357434f7f7dd690428bf -->
> Material depth: `fulltext`
> This view is a structured material projection; asset captions do not prove image interpretation.

> Known gaps:
> - identity: source identity was not independently verified (status=input_attributed)
> - formula: formula region has no recognized formula; flattened orig text is not a faithful formula (status=recognition_required)
> - formula: formula region has no recognized formula; flattened orig text is not a faithful formula (status=recognition_required)
> - formula: formula region has no recognized formula; flattened orig text is not a faithful formula (status=recognition_required)
> - formula: formula region has no recognized formula; flattened orig text is not a faithful formula (status=recognition_required)
> - formula: formula region has no recognized formula; flattened orig text is not a faithful formula (status=recognition_required)
> - formula: formula region has no recognized formula; flattened orig text is not a faithful formula (status=recognition_required)
> - parser_provenance: local model weight hashes were not recorded; package versions, declared model/options and raw output hashes are retained (status=unknown)
> - bibliography: Docling did not label individual references; the bibliography text is retained in ordinary source order, not normalized reference identities (status=unstructured)
> - xml_text_cleanup: removed 30 XML-forbidden controls from derived text; exact native JSON retained (status=normalized)
> - pdf: ordinary body and region index from native Docling; regional enhancement is independent (status=docling_basic)

> Auxiliary metadata retained separately from publisher body:
> - observed_metadata: sources/observed-metadata-5b2b09d42efe.json; use only as supplied metadata/snippet evidence

[block-5885a105c4635334e140] © K. Shastri and F. Monticone, Published by EDP Sciences, 2022

[block-efdde530c175f09c7fec] https://doi.org/10.1051/epjam/2022012

[block-45747694cb6d4e62ed0f] Metamaterials for Novel Wave Phenomena in Microwaves, Optics, and Mechanics

[block-306f4aa447aa98d04189] Figure image region; pixels have not been interpreted.

[block-d82d9fd452c08a8c36cf] Available online at:

[block-da943cec5ce626686a30] epjam.edp-open.org

[block-c8e129e82d2ec7b42211] RESEARCH ARTICLE

## Bandwidth bounds for wide-field-of-view dispersion-engineered achromatic metalenses

## [block-7bd999e16c7af97cacf2] Bandwidth bounds for wide-field-of-view dispersion-engineered achromatic metalenses

[block-6b53c1581775d11ca83b] Kunal Shastri * and Francesco Monticone

[block-c181053f1b19f353869d] Figure image region; pixels have not been interpreted.

[block-2e97a6eff2bd2a9126ca] School of Electrical and Computer Engineering, Cornell University, Ithaca, NY 14853, USA

[block-b2921dc0c65b614ed532] Received: 22 November 2021 / Accepted: 19 April 2022

[block-42952c69a6fdf0453edd] Abstract. Optical systems with wide field-of-views (FOV) are crucial for many applications such as high performance imaging, optical projection, augmented/virtual reality, and miniaturized medical imaging tools. Typically, aberration-free imaging with a wide FOV is achieved by stacking multiple refractive lenses (as in a "fisheye" lens), adding to the size and weight of the optical system. Single metalenses designed to have a wide FOV have the potential to replace these bulky imaging systems and, moreover, they may be dispersion engineered for spectrally broadband operation. In this paper, we derive a fundamental bound on the spectral bandwidth of dispersionengineered wide-FOVachromaticmetalenses. We show that for metalenses with a relatively large numerical aperture (NA), there is a tradeoff between the maximum achievable bandwidth and the FOV; interestingly, however, the bandwidth reduction saturates beyond a certain FOV that depends on the NA of the metalens. These findings may provide important information and insights for the design of future wide-FOV achromatic flat lenses.

[block-bc7bc9f939c135e9c7fc] Keywords: metalens / meta-optics / metasurface / dispersion engineering

## [block-64667178ef2427e99e0b] 1 Introduction

[block-f91117d1338bfd92ac5c] For more than the last three hundred years, optical systems have mostly relied on refractive elements, in the form of lenses for focusing/imaging applications. From a wave optics perspective, these lenses are based on the principle that a continuous phase profile can be added to the incident wavefront through propagation in a dielectric material of inhomogenous thickness. Although dielectric diffractive lenses have a flatter and thinner profile than conventional lenses, as they introduce phase modulo 2p, this phase is still obtained in the same way as in a refractive lens, through wave propagation in a dielectric material of varying thickness [1-4].

[block-7a2bb50408768cdd0bef] In recent decades, new intriguing opportunities in lens design have emerged with the introduction of metasurfaces, flat and planar optical components that can abruptly change the phase of the incident wavefront over the distance of a wavelength or less [5,6]. This is achieved not (or not only) through wave propagation inside a dielectric material, but through wave interaction with arrays of subwavelength nano-structures, called meta-atoms, which scatter light with the desired phase shift, allowing for a local control of the wavefront with great flexibility [7,8]. Lenses designed using metasurfaces, called metalenses, offer flat, compact and lightweight alternatives to conventional lenses, features that are especially important in consumer electronics such as high-performance smartphone cameras and augmented/virtual reality glasses [9-11]. By engineering the response of complex nanostructures used as meta-atoms, metalenses have also been shown to enable additional functionalities, such as polarization control, that cannot be easily achieved using conventional refractive and diffractive lenses [12,13].

[block-02844e8b3039482825d2] * e-mail: kks75@cornell.edu

[block-8bcaf3fe2433fb156f0b] Unfortunately, the abrupt phase shift introduced by metasurfaces is typically strongly dependent on the wavelength of the incident light making it challenging to design broadband metalenses. An elegant strategy to overcome this limitation is to develop a library of metaunits that provide, not only the desired phase pattern, but also the appropriate group delay and group delay-dispersion, as recently proposed in references [14-18]. This can be illustrated by considering a metasurface that changes the phase of an incident plane wave to a generic phase profile given by f (r, v) = z (r) v/c, where v and c are the angular frequency and speed of light in the surrounding medium, respectively, and the function z (r) has units of length. For example, for a linear phase gradient that redirects an incident beam at an angle a, we have z (r)=rsin(a), and, for a metalens with focal length F that focuses normally incident light, z r ð Þ ¼ ðF 2 þ r 2 Þ 1=2 F , where r is the in-plane linear/ radial coordinate, respectively. For an ideal broadband achromatic metasurface, the meta-atoms at each location on the metasurface must be chosen to perfectly satisfy the above phase requirement at every frequency. An alternative, equivalent way to interpret this requirement is that the meta-atoms must be chosen to satisfy the required phase profile at the center frequency vc, along with a constant groupdelayprofile givenby tg r ð Þ ¼ ∂f r; v ð Þ=∂vj vc ¼ z r ð Þ=c, and a group-delay dispersion equal to zero (achromatic wavefront manipulation, for example achromatic focusing or deflection, would still be obtained even if the group delay dispersion is non-zero, as long as it remains spatially constant, but pulses would be distorted). Metalenses designed using this method, implementing the required phase and group-delay profile, are called dispersion-engineered achromatic metalenses, and they can correct for chromatic aberrations over a continuous frequency range centered around the chosen center frequency, as originally demonstrated in [16].

[block-c49e3bd88e30251d93d9] Recently, by drawing parallels between a dispersionengineered achromatic metalens and a series of delay lines and phase shifters, it was shown in reference [19] that the range of group delays that can be implemented by a metaatom cannot be arbitrary but is bound by delay-bandwidth limitations (the product between group delay and signal bandwidth is fundamentally limited in any linear timeinvariant system [20,21]). Consequently, this places a theoretical upper bound on the bandwidth of the dispersion-engineered metalens, regardless of the chosen meta-atom library. In this paper, we generalize the bandwidth bounds to wide field-of-view (FOV) [22-27] achromatic metalenses. In particular, we show that the maximum achievable bandwidth for dispersion-engineered metalenses decreases not only with an increase in numerical aperture (NA) [19], but also with an increase in the FOV.

## [block-02020ccd98846b6b734e] 2 Results

## [block-6c586eb4edf3f498947c] 2.1 Location of the focal spot

[block-8a1a4160860bdad06a38] One challenge in the design of wide-FOV metalenses is that plane waves incident on the lens at various angles need to be phase shifted by different values in order to avoid aberrations. In particular, a metalens with focal length F that changes the phase of an incoming plane wave to a hyperbolic profile given by,

> [raw formula asset asset-a75621d5d94e1ad4e4c5; representation=xml]
> ```text
<disp-formula id="d00031" data-docling-location="{&quot;bbox&quot;:[99.38223576000004,542.83241462,291.94872755999995,563.13195582],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;docling_primary&quot;:true,&quot;docling_prov&quot;:[{&quot;bbox&quot;:{&quot;b&quot;:230.56904418,&quot;coord_origin&quot;:&quot;BOTTOMLEFT&quot;,&quot;l&quot;:99.38223576000004,&quot;r&quot;:291.94872755999995,&quot;t&quot;:250.86858538},&quot;charspan&quot;:[0,93],&quot;page_no&quot;:2}],&quot;kind&quot;:&quot;formula&quot;,&quot;locations&quot;:[{&quot;bbox&quot;:[99.38223576000004,542.83241462,291.94872755999995,563.13195582],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;charspan&quot;:[0,93],&quot;page&quot;:2,&quot;page_height&quot;:793.7009887695312,&quot;page_rotation&quot;:0,&quot;page_width&quot;:595.2760009765625,&quot;unit&quot;:&quot;pt&quot;}],&quot;order_index&quot;:30,&quot;owner_region_id&quot;:&quot;&quot;,&quot;page&quot;:2,&quot;parser&quot;:&quot;docling&quot;,&quot;parser_config_sha256&quot;:&quot;10d2d9715634989d46627fa0c1192089c9a74156194e517d58958bb8cc9a9f78&quot;,&quot;parser_version&quot;:&quot;2.138.0&quot;,&quot;pdf_sha256&quot;:&quot;60004cedb73ee78a6aa242d636259f4bc696766722613bd4dd7e67679d4e5915&quot;,&quot;projection_version&quot;:&quot;optomind.docling_body.v1&quot;,&quot;recognition_status&quot;:&quot;missing&quot;,&quot;region_id&quot;:&quot;d00031&quot;,&quot;representation&quot;:&quot;unenhanced_pdf_text&quot;,&quot;source_json_pointer&quot;:&quot;/texts/25&quot;,&quot;source_ref&quot;:&quot;#/texts/25&quot;,&quot;text_sha256&quot;:&quot;f26412506107c8c9c994ca24066dbd2e145c580fade5334f5201ff09114da041&quot;}" data-recognition-status="missing"><p>Unrecognized formula region; parser_text: f r; v ð Þ ¼  v c ffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffi F 2 þ r 2 p  F   ; ð1Þ</p></disp-formula>
> ```

[block-62905aa170a81f922dc9] achieves aberration-free focusing only for normally incident light [28]. While some workarounds for this problem exist, such as using a curved (aplanatic) metalens [28] or a curved focal surface [29], these devices are not "flat", negating some of the potential benefits of using metalenses. Thus, in this work we focus on flat-optic systems consisting of a planar metalens and a planar focal surface, as sketched in Figure 1. In this case, while normally incident rays are focused at the center of the focal plane, to create an aberration-free image over a wide FOV, obliquely incident rays need to be focused on the same plane at a different location r0 (a) relative to the center. Moreover, to create an undistorted image of the considered scene, it is necessary that r0 (a)=Ftan(a), where a is the angle of incidence and F is the focal length of the metalens

[block-5a95f5d0bbc67fad01c4] Fig. 1. Schematic of an achromaticmetalens with awide field-ofview. The metalens should be able to transform the planar wavefront incident at any given angle into a spherical wavefront that converges at a point on the focal plane. The location of the focal spot, indicated as r0, necessarily depends on the angle of incidence of the plane wave, but, for achromatic operation, it should not depend on frequency.

[block-ad8f8991d355e9928616] Angle of incidence=α

[block-66826dc5120bdcc77d41] [30]. Note that since the problem is cylindrically symmetric, it is sufficient to use a single polar angle a, with a=0 corresponding to normal incidence, to describe the phase shift. Since this mapping from angle of incidence to focal spot position requires an infinitely large sensor for grazing angles of incidence, another frequently used choice is r0 (a)=Fa, which however introduces significant distortions for large incident angles.

## [block-2701a2ceec1dca23f602] 2.2 Ideal metalens phase profile

[block-fb785ef10dabe3ba8115] To focus a planar wavefront incident at a given angle a on a flat focal plane, the lens must impart a combination of a linear phase gradient given by,

> [raw formula asset asset-37dd84ffc4036130f6cc; representation=xml]
> ```text
<disp-formula id="d00039" data-docling-location="{&quot;bbox&quot;:[370.8269914200001,563.31172646,552.73580662,579.68625582],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;docling_primary&quot;:true,&quot;docling_prov&quot;:[{&quot;bbox&quot;:{&quot;b&quot;:214.01474417999998,&quot;coord_origin&quot;:&quot;BOTTOMLEFT&quot;,&quot;l&quot;:370.8269914200001,&quot;r&quot;:552.73580662,&quot;t&quot;:230.38927353999998},&quot;charspan&quot;:[0,44],&quot;page_no&quot;:2}],&quot;kind&quot;:&quot;formula&quot;,&quot;locations&quot;:[{&quot;bbox&quot;:[370.8269914200001,563.31172646,552.73580662,579.68625582],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;charspan&quot;:[0,44],&quot;page&quot;:2,&quot;page_height&quot;:793.7009887695312,&quot;page_rotation&quot;:0,&quot;page_width&quot;:595.2760009765625,&quot;unit&quot;:&quot;pt&quot;}],&quot;order_index&quot;:38,&quot;owner_region_id&quot;:&quot;&quot;,&quot;page&quot;:2,&quot;parser&quot;:&quot;docling&quot;,&quot;parser_config_sha256&quot;:&quot;10d2d9715634989d46627fa0c1192089c9a74156194e517d58958bb8cc9a9f78&quot;,&quot;parser_version&quot;:&quot;2.138.0&quot;,&quot;pdf_sha256&quot;:&quot;60004cedb73ee78a6aa242d636259f4bc696766722613bd4dd7e67679d4e5915&quot;,&quot;projection_version&quot;:&quot;optomind.docling_body.v1&quot;,&quot;recognition_status&quot;:&quot;missing&quot;,&quot;region_id&quot;:&quot;d00039&quot;,&quot;representation&quot;:&quot;unenhanced_pdf_text&quot;,&quot;source_json_pointer&quot;:&quot;/texts/32&quot;,&quot;source_ref&quot;:&quot;#/texts/32&quot;,&quot;text_sha256&quot;:&quot;fd8ff88777d0fdf0f933c70c505392ee441dec3f353a46e47382c4dd5f8e6bb3&quot;}" data-recognition-status="missing"><p>Unrecognized formula region; parser_text: f l r; v; a ð Þ ¼  v c r sin a ð Þ ð Þ; ð2Þ</p></disp-formula>
> ```

[block-fa6a481018fdf35eaf6d] to redirect the wavefront to the surface normal, and the hyperbolic radial profile given by equation (1) but focusing light at r0 instead of the center of the plane [31],

> [raw formula asset asset-94dd62adb428f65a5048; representation=xml]
> ```text
<disp-formula id="d00041" data-docling-location="{&quot;bbox&quot;:[305.06386180000004,631.0475521200001,552.73717148,669.8239838400001],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;docling_primary&quot;:true,&quot;docling_prov&quot;:[{&quot;bbox&quot;:{&quot;b&quot;:123.87701615999993,&quot;coord_origin&quot;:&quot;BOTTOMLEFT&quot;,&quot;l&quot;:305.06386180000004,&quot;r&quot;:552.73717148,&quot;t&quot;:162.65344787999993},&quot;charspan&quot;:[0,263],&quot;page_no&quot;:2}],&quot;kind&quot;:&quot;formula&quot;,&quot;locations&quot;:[{&quot;bbox&quot;:[305.06386180000004,631.0475521200001,552.73717148,669.8239838400001],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;charspan&quot;:[0,263],&quot;page&quot;:2,&quot;page_height&quot;:793.7009887695312,&quot;page_rotation&quot;:0,&quot;page_width&quot;:595.2760009765625,&quot;unit&quot;:&quot;pt&quot;}],&quot;order_index&quot;:40,&quot;owner_region_id&quot;:&quot;&quot;,&quot;page&quot;:2,&quot;parser&quot;:&quot;docling&quot;,&quot;parser_config_sha256&quot;:&quot;10d2d9715634989d46627fa0c1192089c9a74156194e517d58958bb8cc9a9f78&quot;,&quot;parser_version&quot;:&quot;2.138.0&quot;,&quot;pdf_sha256&quot;:&quot;60004cedb73ee78a6aa242d636259f4bc696766722613bd4dd7e67679d4e5915&quot;,&quot;projection_version&quot;:&quot;optomind.docling_body.v1&quot;,&quot;recognition_status&quot;:&quot;missing&quot;,&quot;region_id&quot;:&quot;d00041&quot;,&quot;representation&quot;:&quot;unenhanced_pdf_text&quot;,&quot;source_json_pointer&quot;:&quot;/texts/34&quot;,&quot;source_ref&quot;:&quot;#/texts/34&quot;,&quot;text_sha256&quot;:&quot;efc6ad6e4734e1f68eb5b4fa0f17dc75f3fcfd15f97fae319c284cd34e1eecc5&quot;}" data-recognition-status="missing"><p>Unrecognized formula region; parser_text: f s r; v; a ð Þ ¼  v c ffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffi F 2 þ r  r0 a ð Þ ð Þ 2 q  ffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffi F 2 þ r0 a ð Þ 2 q   : ð3Þ</p></disp-formula>
> ```

[block-bd3ae40d840d13599e01] Thus, the phase shift that a wide-FOV metalens must introduce for focusing without aberrations is,

> [raw formula asset asset-ad1fbf52a76f32f0deb0; representation=xml]
> ```text
<disp-formula id="d00043" data-docling-location="{&quot;bbox&quot;:[352.46201246000004,712.30145,552.73698902,723.414129],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;docling_primary&quot;:true,&quot;docling_prov&quot;:[{&quot;bbox&quot;:{&quot;b&quot;:70.28687100000002,&quot;coord_origin&quot;:&quot;BOTTOMLEFT&quot;,&quot;l&quot;:352.46201246000004,&quot;r&quot;:552.73698902,&quot;t&quot;:81.39954999999998},&quot;charspan&quot;:[0,54],&quot;page_no&quot;:2}],&quot;kind&quot;:&quot;formula&quot;,&quot;locations&quot;:[{&quot;bbox&quot;:[352.46201246000004,712.30145,552.73698902,723.414129],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;charspan&quot;:[0,54],&quot;page&quot;:2,&quot;page_height&quot;:793.7009887695312,&quot;page_rotation&quot;:0,&quot;page_width&quot;:595.2760009765625,&quot;unit&quot;:&quot;pt&quot;}],&quot;order_index&quot;:42,&quot;owner_region_id&quot;:&quot;&quot;,&quot;page&quot;:2,&quot;parser&quot;:&quot;docling&quot;,&quot;parser_config_sha256&quot;:&quot;10d2d9715634989d46627fa0c1192089c9a74156194e517d58958bb8cc9a9f78&quot;,&quot;parser_version&quot;:&quot;2.138.0&quot;,&quot;pdf_sha256&quot;:&quot;60004cedb73ee78a6aa242d636259f4bc696766722613bd4dd7e67679d4e5915&quot;,&quot;projection_version&quot;:&quot;optomind.docling_body.v1&quot;,&quot;recognition_status&quot;:&quot;missing&quot;,&quot;region_id&quot;:&quot;d00043&quot;,&quot;representation&quot;:&quot;unenhanced_pdf_text&quot;,&quot;source_json_pointer&quot;:&quot;/texts/36&quot;,&quot;source_ref&quot;:&quot;#/texts/36&quot;,&quot;text_sha256&quot;:&quot;391e80b28c5efbd0ac28a683b14b5b20330c171513ea695531b9087b779d6ec9&quot;}" data-recognition-status="missing"><p>Unrecognized formula region; parser_text: f r; v; a ð Þ ¼ f l r; v; a ð Þ þ f s r; v; a ð Þ: ð4Þ</p></disp-formula>
> ```

[block-14459b436a20a87db87d] Fig. 2. Ideal spatial phase profile for focusing an obliquely incident plane wave on a planar focal plane at a distance F from a planar metalens. The ideal phase profile for any given angle of incidence can be expressed as the sum of a linear phase gradient (a) to redirect the wave to the surface normal and a hyperbolic phase profile (b) to focus the wave. In (a)-(c) phase profiles for different polar angles of incidence, a, are represented using different colors and a= 0 corresponds to normal incidence. One particular phase profile for angle of incidence equal to 30 deg is highlighted with a bolder line. (d), (e) Ideal phase profile, as a function of the angle of incidence, a, for two different choices for the location of the focal spot: (d) r0=Ftan(a) and (e) r0=Fa. The phase is normalized by the product of the wavenumber, k0 = v/c, and the focal length of the metalens, F, so that both plots can also be interpreted as the normalized group delay (ctg=F ¼ c∂f r; v ð Þ=∂vj =F) required for achromatic focusing. In all plots ' r ' is the radial coordinate on the metalens.

[block-b960e2d172d2d2082c5c] vc

[block-6764ece7e59e42e8449b] The spatial dependence of the two individual phase profiles and the resultant net phase shift for r0 (a)=Ftan(a) are plotted in Figures 2a-2c. Furthermore, the calculated net phase profiles for two different choices of r0 (a) are shown as a function of the angle of incidence in Figures 2d and 2e. Importantly, it can be seen that the ideal phase profile has a strong dependence on the direction of the incident light and, for a metalens of finite lateral extent, the required range of phase shifts does not increase monotonically with the angle of incidence. In fact, while the required total phase range would be the same regardless of incident angle for an ideal, infinitely extended metalens, for a finite metalens the phase range to be imparted is minimal for grazing angles of incidence, as seen in Figures 2c and 2d, and is maximum at a certain angle that depends on the maximum radius of the metalens.

[block-7300da1fb43f4bb43c3d] An important point to note is that a "local" metasurface can only introduce a position-dependent phase shift and not an angle-dependent shift. Hence, while a local metasurface can be used to focus light incident at a specified angle, using the angle-appropriate phase profile above, it will form a distorted and aberrated image for light incident at other angles. On the other hand, a lens with a FOV equal to 2a0 should be able to focus light from all angles between a0 and a0 without any aberrations.

[block-c4ff01804825db9bb35a] The easiest way to achieve an angle-dependent phase shift with local metasurfaces is to consider a system with at least two surfaces separated by a gap. The gap here is crucial since it utilizes the angle-dependent phase accumulated by plane waves propagating through a free-space (or dielectric) slab to create an overall angle-dependent transmission phase. In practice, this has been implemented using two metasurfaces separated by a certain distance [22,32,33] or a single metasurface preceded by a circular aperture [25,27,30,31,34]. The aperture followed by the free-space gap maps incident beams from different angles onto separate (or minimally overlapping) spatial regions on the local metasurface, which can then implement the necessary spatial phase shift (a nice example of this design strategy is demonstrated in Ref. [31]). In general, however, the bandwidth limitations for dispersion-engineered achromatic metalenses that will be derived in the next section are not affected by the presence of the aperture, since the required range of group delays remains unchanged.

[block-497d0ee92ff0b2252f80] To achieve an angle-dependent phase shift with a single metasurface, it would instead be necessary to design angledependent meta-atoms providing different phase shifts for different angles [35] or, more broadly, "nonlocal metasurfaces" with an overall angle-dependent response [36-43]. As further discussed in the Discussion section, nonlocal metasurfaces may provide some intriguing opportunities to overcome the limitations discussed in the following, which are based on the assumption of local interactions between incident the wave and meta-atoms.

## [block-5cb203d0459893020895] 2.3 Maximum achievable bandwidth

[block-2f373ec932157f3d488b] Next, we proceed to calculate the fundamental bandwidth bounds for wide-FOV metalenses that have been designed, through dispersion engineering, for broadband achromatic operation (i.e., for diffraction-limited focusing at the same distance over a broad frequency range). As discussed in the Introduction, the meta-atoms composing these structures are designed to implement not only the phase shift given by equation (4) at the center frequency, but also the required group delay for broadband focusing. The group delay required for a pulse incident on the wide-FOV metalens at a location r relative to the center of the lens, and at an angle a, can be calculate by taking the angular frequency derivative of equation (4),

> [raw formula asset asset-0b6b572a837b78635e9d; representation=xml]
> ```text
<disp-formula id="d00057" data-docling-location="{&quot;bbox&quot;:[42.9732,408.85077232000003,291.94959728,455.86415000000005],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;docling_primary&quot;:true,&quot;docling_prov&quot;:[{&quot;bbox&quot;:{&quot;b&quot;:337.83684999999997,&quot;coord_origin&quot;:&quot;BOTTOMLEFT&quot;,&quot;l&quot;:42.9732,&quot;r&quot;:291.94959728,&quot;t&quot;:384.85022768},&quot;charspan&quot;:[0,261],&quot;page_no&quot;:4}],&quot;kind&quot;:&quot;formula&quot;,&quot;locations&quot;:[{&quot;bbox&quot;:[42.9732,408.85077232000003,291.94959728,455.86415000000005],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;charspan&quot;:[0,261],&quot;page&quot;:4,&quot;page_height&quot;:793.7009887695312,&quot;page_rotation&quot;:0,&quot;page_width&quot;:595.2760009765625,&quot;unit&quot;:&quot;pt&quot;}],&quot;order_index&quot;:56,&quot;owner_region_id&quot;:&quot;&quot;,&quot;page&quot;:4,&quot;parser&quot;:&quot;docling&quot;,&quot;parser_config_sha256&quot;:&quot;10d2d9715634989d46627fa0c1192089c9a74156194e517d58958bb8cc9a9f78&quot;,&quot;parser_version&quot;:&quot;2.138.0&quot;,&quot;pdf_sha256&quot;:&quot;60004cedb73ee78a6aa242d636259f4bc696766722613bd4dd7e67679d4e5915&quot;,&quot;projection_version&quot;:&quot;optomind.docling_body.v1&quot;,&quot;recognition_status&quot;:&quot;missing&quot;,&quot;region_id&quot;:&quot;d00057&quot;,&quot;representation&quot;:&quot;unenhanced_pdf_text&quot;,&quot;source_json_pointer&quot;:&quot;/texts/49&quot;,&quot;source_ref&quot;:&quot;#/texts/49&quot;,&quot;text_sha256&quot;:&quot;c1e454a7afbe231f100ea80e12a59db9c0d8500c1c7b41d48ae625bb8a018a4d&quot;}" data-recognition-status="missing"><p>Unrecognized formula region; parser_text: tgðr; aÞ ¼  F c r F sinðaÞ þ ffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffi 1 þ r F  r0ðaÞ F  2 s  ffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffiffi 1 þ r0ðaÞ 2 F s 2 4 3 5: ð5Þ</p></disp-formula>
> ```

[block-98271eb5549a107bffa8] In this equation the relative group delay is negative at any position r≠0, which can be interpreted as a time advance for a pulse at the edges of the metalens with respect to an undelayed pulse at the center, or as a (more physical) time delay at the center with respect to an undelayed pulse at the edges. Importantly, adding a global (i.e., position-independent) time delay is irrelevant for our purposes. Note that the plots in Figures 2d and 2e can also be used as representations of the group delay profile. This is because the spatial phase profile is proportional to the group delay profile (as can be seen by comparing Eqs. (4) and (5)) and they can be made numerically equal using an appropriate choice of normalization. The plot in Figure 2d corresponds to the group delay calculated for a choice of focal spot location r0=F tan(a), and 2e for r0=Fa.

[block-883314943818740d5943] Since the thickness of typical metalenses is of the order of the wavelength, this group delay is primarily acquired during longitudinal propagation through the metalens (namely, delay acquired through lateral propagation can be neglected, as in [19]). Thus, each point on the metalens can be modeled as a 1D delay line designed to implement the local delay given by equation (5). This group delay is typically introduced by using resonant meta-atoms or, alternatively, meta-atoms acting as truncated waveguide segments with engineered dispersion. In the case of a structure supporting a single resonance, a simple study of the dynamics of the mode amplitude shows that the total rate of loss g, due to absorption and radiation, determines both the full-width-at-half-maximum bandwidth of the resonance D v = 2g and the mode lifetime t = 1/g, the latter of which corresponds to the maximum delay achievable through the interaction with the cavity. The delaybandwidth product is therefore, D T D v=2, for any single-mode resonant cavity [44] and it can be easily verified that, for multi-resonator systems, the product scales with the number of resonators [45,46]. Furthermore, more general limits on the delay-bandwidth product can be derived for generic 1D systems, applicable also to nonresonant waveguiding structures. A particularly general expression for this limit was derived by Miller in [20,21]: D T D v k ¼ Lvch=2 ffiffiffi 3 p c, where L is the length of the structure, vc is the central frequency, and h is the maximum contrast in relative permittivity at any frequency within the considered bandwidth and any point within the structure. While the derivation of this result is rather involved, the bound can be intuitively understood, in the case of dielectric waveguides, considering the dispersion diagram (band diagram) of the waveguide modes: since the guided-mode bands are confined between the light lines defined by the permittivities of the dielectric waveguide and the surrounding material, reducing the group velocity (i.e., increasing the group delay) by flattening a band between these two lines necessarily implies a reduction of the bandwidth over which the desired group velocity can be achieved. Applied to our case of interest, these bounds on the delay-bandwidth product allow limiting the maximum bandwidth over which a metaatom is able to implement a certain group delay, and, in turn, the maximum operating bandwidth of the entire dispersion-engineered achromatic metalens. For further details on the relevance of the delay-bandwidth product for metalenses and metasurfaces, see references [4,19,43].

[block-a701074bf1bcf8b0b496] Based on the above considerations, the maximum frequency range for achromatic operation for a metalens with a FOV equal to 2a0 can then be calculated by finding from equation (5) the maximum required group delay within the angular range a0aa0, and substituting it in the delay-bandwidth bound,

> [raw formula asset asset-800c0484ad89ddf44535; representation=xml]
> ```text
<disp-formula id="d00061" data-docling-location="{&quot;bbox&quot;:[396.79413972,561.4983500000001,552.7367161799999,577.87215582],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;docling_primary&quot;:true,&quot;docling_prov&quot;:[{&quot;bbox&quot;:{&quot;b&quot;:215.82884418000003,&quot;coord_origin&quot;:&quot;BOTTOMLEFT&quot;,&quot;l&quot;:396.79413972,&quot;r&quot;:552.7367161799999,&quot;t&quot;:232.20264999999995},&quot;charspan&quot;:[0,20],&quot;page_no&quot;:4}],&quot;kind&quot;:&quot;formula&quot;,&quot;locations&quot;:[{&quot;bbox&quot;:[396.79413972,561.4983500000001,552.7367161799999,577.87215582],&quot;bbox_origin&quot;:&quot;TOPLEFT&quot;,&quot;charspan&quot;:[0,20],&quot;page&quot;:4,&quot;page_height&quot;:793.7009887695312,&quot;page_rotation&quot;:0,&quot;page_width&quot;:595.2760009765625,&quot;unit&quot;:&quot;pt&quot;}],&quot;order_index&quot;:60,&quot;owner_region_id&quot;:&quot;&quot;,&quot;page&quot;:4,&quot;parser&quot;:&quot;docling&quot;,&quot;parser_config_sha256&quot;:&quot;10d2d9715634989d46627fa0c1192089c9a74156194e517d58958bb8cc9a9f78&quot;,&quot;parser_version&quot;:&quot;2.138.0&quot;,&quot;pdf_sha256&quot;:&quot;60004cedb73ee78a6aa242d636259f4bc696766722613bd4dd7e67679d4e5915&quot;,&quot;projection_version&quot;:&quot;optomind.docling_body.v1&quot;,&quot;recognition_status&quot;:&quot;missing&quot;,&quot;region_id&quot;:&quot;d00061&quot;,&quot;representation&quot;:&quot;unenhanced_pdf_text&quot;,&quot;source_json_pointer&quot;:&quot;/texts/53&quot;,&quot;source_ref&quot;:&quot;#/texts/53&quot;,&quot;text_sha256&quot;:&quot;97ab2b66661e38a7528f90cf344448bc27b3956d10dfc8605cbbdb472294e5bf&quot;}" data-recognition-status="missing"><p>Unrecognized formula region; parser_text: D v  k DT max : ð6Þ</p></disp-formula>
> ```

[block-4525c0ab5f8a7ee95ca5] The group delay range that the metalens should provide widens for larger lenses, as larger delays need to be implemented at the center (r=0) to compensate for the additional time taken by a pulse arriving to the focal spot from the edges (r=R). The required delay range also widens for larger FOV (wider range of angles of incidence), since, intuitively, an even greater delay needs to be compensated across the metalens as the angle of incidence increases. However, this is true only up to a point. Indeed, the range of required group delays across a finite metalens does not monotonically widens as the angle of incidence increases owing to the non-monotonic increase of phase range with angle that we discussed in the previous section

[block-61061ff5cf1181417066] Fig. 3. Calculated bandwidth bounds for wide-FOV dispersion-engineered achromatic metalenses. In (a) and (c), the bound is calculated for a focal spot location given by r0=Ftan(a) and in (b) and (d) for r0=Fa. To represent the bound in a general way applicable to vastly different metalenses, the bandwidth is normalized by the factor k, the focal length of the metalens F, and the speed of light c. The bandwidth axis in (a) and (b) has been scaled logarithmically for clarity. In all the panels, NA, nb, and FOV are the numerical aperture, background refractive index, and the field of view, respectively.

[block-cdfab448ee34e6a3fea6] (see Figs. 2d,2e). Taking this into account, the greatest overall delay that the metasurface must be able to implement can be calculated as D Tmax=tg (r=0, a=0) tg (r=R, a=a * ), where a * is the angle, within the range a0aa0, for which the group-delay range across the metalens is widest (essentially this is the difference between the maximum and the minimum in the contour plots in Figs. 2d,2e, for any angle a and position r, when these plots are interpreted as a representation of the normalized delay). Since there is no simple expression for D Tmax, we evaluate it numerically and then use it in equation (6) to determine the bandwidth limit for two different choices of focal position r0. The results are plotted in Figure 3.

[block-954eb9a994eb5f4efda2] As a function of the numerical aperture (NA) of the metalens, Figures 3a and 3b show a clear trend of narrower achievable bandwidths for metalenses with larger NA, which is expected, since a metalens with larger NA has a larger delay to compensate between the center and the edges, consistent with [19]. Here, we only plotted the results for moderately large NA because, for smaller values, the bandwidth bound for any FOV approaches the normal-incidence bound derived in [19]. Most importantly, it can also be seen from these plots that the bandwidth limit shrinks for metalenses with larger FOV. However, surprisingly, this trend saturates for a certain FOV, depending on the NA and the choice of focal spot position, as seen in Figures 3c and 3d. This implies, for example, that a dispersion engineered metalens with a ultra wide FOV= 180 deg could, in principle, have the same spectral bandwidth as a metalens with a relatively narrower FOV=60 deg, for a fixed NA=0.7nb. This result is a consequence of the previously mentioned observation that for a finite metalens and a fixed FOV, the widest group-delay range that the metalens should implement is not always for light incident at the maximum oblique angle, implying that beyond a certain FOV (dependent on NA), the maximum allowed bandwidth does not further decrease. Furthermore, this trend is observed for both choices of focal spot position, as seen in Figures 3c and 3d.

## [block-86718176df2618ce605c] 3 Discussion

[block-463c314fbc115359c607] In the recent literature, moderately broadband achromatic metalenses have been reported with remarkably large FOVs, as well as sufficiently low aberrations resulting in a high Strehl ratio [25,27,30,31,34]. In particular, a complete 180 deg FOV over a relatively wide fractional bandwidth of 0.2 has been reported in [34]. However, the NA in this design is limited to 0.24 for which our bounds are less relevant since, as seen in Figures 3a and 3b, an appreciable decrease in bandwidth as a function of FOV is only seen for metalenses with a relatively large NA. In this context, it would be interesting to see how future wide-FOV metalens designs with larger NA compare with our bounds.

[block-69b7d0d80dc5c708d213] In relation to the bandwidth bounds derived above, it is also instructive to consider structures that can potentially break some of the assumptions in the derivation and thereby overcome the bound. Indeed, the bandwidth bounds derived here apply to the spatial phase profile given by equation (4), which is specific to the case of diffraction-limited focusing on a flat focal plane. For other imaging system designs, such as focusing on curved focal surfaces, the resultant bound will have to be modified depending on the specific spatial phase profile of the considered metalens and the desired imaging functionality. In addition, relaxing the constraint on diffraction-limited resolution and Strehl ratio would imply that a certain level of error in the implemented phase/group delay profile would be acceptable, which would therefore relax the resulting bandwidth bounds, as also demonstrated in reference [19].

[block-19377701cf3bbbb38e04] Moreover, since the considered limits to the delaybandwidth product strictly only apply to 1D structures (the meta-atoms modeled as 1D delay lines), the derived bound will not apply to systems that allow additional group delay due to transverse propagation. For instance, systems consisting of cascaded metasurfaces could take advantage of the lateral dimension between metasurfaces to achieve larger group delays and potentially overcome the bandwidth bounds derived in this paper (however, a generalized version of these bounds should apply also to this type of systems, and its derivation will be the subject of future work). The same argument also applies to wide-FOV imaging systems constructed using bulk metamaterials, or thick volumetric structures obtained, for example, through all-area inverse design [23]. More broadly, we note that the analogy between delay lines and meta-atoms is valid only if the metasurface response is based on the local interaction between fields and meta-atoms at a specific point on the surface. Differently from achromatic metalenses, achromatic diffractive lenses [3] achieve broadband operation through energy redistribution among different diffraction orders for different frequencies, a mechanism that involves the overall surface structure rather a local effect, as discussed in [4]. For this reason, achromatic diffractive lenses, unlike dispersion-engineered metalenses, are not limited by delay-bandwidth considerations, but suffer from other limitations especially in terms of focusing power [4]. This general discussion also hints at the large potential of nonlocal metasurfaces [36-43] an emerging class of metasurfaces specifically designed to exhibit strong nonlocal effects to potentially achieve performance metrics that are unattainable with more conventional designs.

[block-a68409c370cb09211451] To conclude, in this paper we have generalized our previous work on metalens bandwidth bounds to dispersion-engineered achromatic metalenses with a wide FOV. Single element (meta)lenses with large spectral and angular bandwidth are potentially of immense importance for a wide range of applications, including high-performance smartphone imaging, optical projection, endoscopic optical imaging, and augmented/virtual reality. In this context, the bandwidth bounds derived in this paper may provide important information and insights toward the design of next-generation metalenses with, simultaneously, wide FOV, high resolution, and broad spectral bandwidth.

[block-f85b8e2caa05696d989c] We acknowledge support from the National Science Foundation (NSF) with Grant No. 1741694, and the Air Force Office of Scientific Research with Grant No. FA9550-19-1-0043.

## [block-941fe2a8661952524739] References

[block-d80f2883632d2f887bcf] D.W. Sweeney, G.E. Sommargren, Harmonic diffractive lenses, Appl. Opt. 34, 2469 (1995)

[block-e018681ec3c50f49014f] J. Engelberg, U. Levy, The advantages of metalenses over diffractive lenses, Nat. Commun. 11, 1991 (2020)

[block-d6c86d325cbe42628179] S. Banerji, M. Meem, A. Majumder, F.G. Vasquez, B. Sensale-Rodriguez, R. Menon, Imaging with at optics: metalenses or diffractive lenses?, Optica 6, 805 (2019)

[block-8835cbe6a6046ea86e43] J. Engelberg, U. Levy, Achromatic at lens performance limits, Optica 8, 834 (2021)

[block-0e6b0590d4ea37afd4c0] F. Ding, A. Pors, S.I. Bozhevolnyi, Gradient meta-surfaces: a review of fundamentals and applications, Rep. Prog. Phys. 81, 026401 (2018)

[block-2d6d19163273448b1584] P. Genevet, F. Capasso, F. Aieta, M. Khorasaninejad, R. Devlin, Recent advances in planar optics: from plasmonic to dielectric metasurfaces, Optica 4, 139 (2017)

[block-6c11d729ba39de3ca50b] M. Decker, W.T. Chen, T. Nobis, A.Y. Zhu, M. Khorasaninejad, Z. Bharwani, F. Capasso, J. Petschulat, Imaging performance of polarization-insensitive metalenses, ACS Photonics 6, 1493 (2019)

[block-6f1a1b859350f3f67ccc] A. Arbabi, Y. Horie, A.J. Ball, M. Bagheri, A. Faraon, Subwavelength-thick lenses with high numerical apertures and large efficiency based on high-contrast transmitarrays, Nat. Commun. 6, 7069 (2015)

[block-4fde2bf32b40118ad641] P. Lalanne, P. Chavel, Metalenses at visible wavelengths: past, present, perspectives: metalenses at visible wavelengths: past, present, perspectives, Laser Photon. Rev. 11, 1600295 (2017).

[block-d4aa40ce9ecf11f75ae4] M.L. Tseng, H.-H. Hsiao, C.H. Chu, M.K. Chen, G. Sun, A.-Q. Liu, D.P. Tsai, Metalenses: advances and applications, Adv. Opt. Mater. 6, 1800554 (2018)

[block-23b856ba2226ec6144ac] M. Khorasaninejad, F. Capasso, Metalenses: versatile functional photonic components, Science 358, eaam8100 (2017)

[block-190b0e9cf9588dcf1bc9] N. Yu, F. Capasso, Flat optics with designer meta-surfaces, Nat. Mater 13, 139 (2014)

[block-3bf35a6e676b75be246a] B. Yu, J. Wen, L. Chen, L. Zhang, Y. Fan, B. Dai, S. Kanwal, D. Lei, D. Zhang, Polarization-independent highly efficient generation of Airy optical beams with dielectric metasurfaces, Photon. Res. 8, 1148 (2020)

[block-537b1615f8cb54d16f8d] W.T. Chen, A.Y. Zhu, F. Capasso, Flat optics with dispersion-engineered metasurfaces, Nat. Rev. Mater. 5, 604 (2020)

[block-65f03967006e37f5151d] E. Arbabi, A. Arbabi, S.M. Kamali, Y. Horie, A. Faraon, Controlling the sign of chromatic dispersion in diffractive optics with dielectric metasurfaces, Optica 4, 625 (2017)

[block-c3f5e3d9a313bf191df2] S. Shrestha, A.C. Overvig, M. Lu, A. Stein, N. Yu, Broadband achromatic dielectric metalenses, Light Sci. Appl. 7, 85 (2018)

[block-a90c85a71280fcc0817c] W.T. Chen, A.Y. Zhu, V. Sanjeev, M. Khorasaninejad, Z. Shi, E. Lee, F. Capasso, A broadband achromatic metalens for focusing and imaging in the visible, Nat. Nanotech. 13, 220 (2018)

[block-eaa3d0e63d9961debbf2] W.T. Chen, A.Y. Zhu, J. Sisler, Z. Bharwani, F. Capasso, A broadband achromatic polarization-insensitive metalens consisting of anisotropic nanostructures, Nat. Commun. 10, 355 (2019)

[block-abc72e6c706ac0f25ce9] F. Presutti, F. Monticone, Focusing on bandwidth: achromatic metalens limits, Optica 7, 624 (2020)

[block-119c0b1a66487bd65de4] D.A.B. Miller, Fundamental Limit to Linear One-Dimensional Slow Light Structures, Phys. Rev. Lett. 99, 203903 (2007)

[block-e8f0249c9101ca31a220] D.A.B. Miller, Fundamental limit for optical components, J. Opt. Soc. Am. B 24, A1 (2007)

[block-839a17752f5d569a41d7] Z. Huang, M. Qin, X. Guo, C. Yang, S. Li, Achromatic and wide-field metalens in the visible region, Opt. Express 29, 13542 (2021)

[block-1cca25c2332b5278ebec] Z. Lin, C. Roques-Carmes, R.E. Christiansen, M. Soljai, S.G. Johnson, Computational inverse design for ultra-compact single-piece metalenses free of chromatic and angular aberration, Appl. Phys. Lett. 118, 041104 (2021)

[block-b3b015b24dae1c0220c1] A. Martins, K. Li, J. Li, H. Liang, D. Conteduca, B.-H.V. Borges, T.F. Krauss, E.R. Martins, On metalenses with arbitrarily wide field of view, ACS Photonics 7, 2073 (2020)

[block-a46b6f973ac6f938040c] C.-Y. Fan, C.-P. Lin, G.-D.J. Su, Ultrawide-angle and highefficiency metalens in hexagonal arrangement, Sci. Rep. 10, 15677 (2020)

[block-1b39edb01d88386a5279] C. Chen, P. Chen, J. Xi, W. Huang, K. Li, L. Liang, F. Shi, J. Shi, On-chip monolithic wide-angle field-of-view metalens based on quadratic phase profile, AIP Adv. 10, 115213 (2020)

[block-09ef74f2b4c44cce5365] Y. Guo, X. Ma, M. Pu, X. Li, Z. Zhao, X. Luo, High-efficiency and wide-angle beam steering based on catenary optical fields in ultrathin Metalens, Adv. Opt. Mater. 6, 1800592 (2018)

[block-c701c9df3051ec4ee514] F. Aieta, P. Genevet, M. Kats, F. Capasso, Aberrations of at lenses and aplanatic metasurfaces, Opt. Express 21, 31530 (2013)

[block-18bf8d9aad6c2a8df6d3] J. Hunt, N. Kundtz, N. Landy, V. Nguyen, T. Perram, A. Starr, D.R. Smith, Broadband wide angle lens implemented with dielectric metamaterials, Sensors 11, 7982 (2011)

[block-6519984fa1e559ecf596] A. Kalvach, Z. Szab, Aberration-free at lens design for a wide range of incident angles, J. Opt. Soc. Am. B 33, A66 (2016)

[block-7128691c662fa8d98710] M.Y. Shalaginov, S. An, F. Yang, P. Su, D. Lyzwa, A.M. Agarwal, H. Zhang, J. Hu, T. Gu, Single-element diffractionlimited fisheye metalens, Nano Lett. 20, 7429 (2020)

[block-11215d1ac04e3e033223] A. Arbabi, E. Arbabi, S.M. Kamali, Y. Horie, S. Han, A. Faraon, Miniature optical planar camera based on a wideangle metasurface doublet corrected for monochromatic aberrations, Nat. Commun. 7, 13682 (2016)

[block-7efd697cb9c31337ade4] B. Groever, W.T. Chen, F. Capasso, Meta-lens doublet in the visible region, Nano Lett. 17, 4902 (2017)

[block-9746bd3ea11959c96ba8] F. Yang, F. Yang, S. An, M.Y. Shalaginov, H. Zhang, C. Rivero-Baleine, J. Hu, J. Hu, T. Gu, T. Gu, Design of broadband and wide-field-of-view metalenses, Opt. Lett. 46, 5735 (2021).

[block-13340846021a43bd9929] S.M. Kamali, E. Arbabi, A. Arbabi, Y. Horie, M. FarajiDana, A. Faraon, Angle-multiplexed metasurfaces: encoding independent wavefronts in a single metasurface under different illumination angles, Phys. Rev. X 7, 041056 (2017)

[block-3aa683131773f794923e] A. Silva, F. Monticone, G. Castaldi, V. Galdi, A. Al, N. Engheta, Performing mathematical operations with metamaterials, Science 343, 160 (2014)

[block-d1f4465bfa4951b75df4] F. Monticone, C.A. Valagiannopoulos, A. Alu, Parity-time symmetric nonlocal metasurfaces: all-angle negative refraction and volumetric imaging, Phys. Rev. X 6, 041018 (2016)

[block-8dc69fe43c373a41b758] A.C. Overvig, S.A. Mann, A. Al, Thermal metasurfaces: complete emission control by combining local and nonlocal light-matter interactions, Phys. Rev. X 11, 021050 (2021)

[block-195aa7e1ed908f06b33c] A.C. Overvig, S.C. Malek, N. Yu, Multifunctional nonlocal metasurfaces, Phys. Rev. Lett. 125, 017402 (2020)

[block-325b3073cd7eba08046b] O. Reshef, M.P. DelMastro, K.K. Bearne, A.H. Alhulaymi, L. Giner, R.W. Boyd, J.S. Lundeen, An optic to replace space and its application towards ultra-thin imaging systems, Nat. Commun. 12, 1 (2021)

[block-5c294af7a3736f501cc5] C. Guo, H. Wang, S. Fan, Squeeze free space with nonlocal at optics, Optica 7, 1133 (2020)

[block-59aa1109b09d85388ca0] A. Chen, F. Monticone, Dielectric nonlocal metasurfaces for fully solid-state ultrathin optical systems, ACS Photon. 8, 1439 (2021)

[block-54c74030749a2dfe4b19] K. Shastri, O. Reshef, R.W. Boyd, J.S. Lundeen, F. Monticone, To what extent can space be compressed? Bandwidth limits of spaceplates, arXiv:2201.11340 (2022)

[block-61516bfb150e91379086] S.A. Mann, D.L. Sounas, A. Al, Nonreciprocal cavities and the timebandwidth limit, Optica 6, 104 (2019)

[block-15e31c8ab36393df8a53] Z. Wang, S. Fan, Compact all-pass filters in photonic crystals as the building block for high-capacity optical delay lines, Phys. Rev. E 68, 066616 (2003)

[block-76392b7c1d6dc2bb1b76] R. Tucker, P.-C. Ku, C. Chang-Hasnain, Slow-light optical buffers: capabilities and fundamental limitations, J. Lightwave Technol. 23, 4046 (2005)

[block-83d3a6bc8712099d9423] Cite this article as: Kunal Shastri, Francesco Monticone, Bandwidth bounds for wide-field-of-view dispersion-engineered achromatic metalenses, EPJ Appl. Metamat. 9, 16 (2022)
