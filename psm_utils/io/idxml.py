"""
Interface with OpenMS idXML PSM files.


Notes
-----

* idXML supports multiple peptide hits (identifications) per spectrum. Each peptide hit
  is parsed as an individual :py:class:`~psm_utils.psm.PSM` object.

"""


from __future__ import annotations

import logging
from typing import Union, Iterable, Tuple, List
import re
from pathlib import Path

import pyopenms as oms

from psm_utils.io._base_classes import ReaderBase, WriterBase
from psm_utils.psm import PSM
from psm_utils.psm_list import PSMList
from psm_utils.exceptions import PSMUtilsException

logger = logging.getLogger(__name__)

# Patterns to match open and closed round/square brackets
MOD_PATTERN = re.compile(r"\(((?:[^)(]+|\((?:[^)(]+|\([^)(]*\))*\))*)\)")
MOD_PATTERN_NTERM = re.compile(r"^\.\[((?:[^][]+|\[(?:[^][]+|\[[^][]*\])*\])*)\]")
MOD_PATTERN_CTERM = re.compile(r"\.\[((?:[^][]+|\[(?:[^][]+|\[[^][]*\])*\])*)\]$")

# Extracted from the OpenMS PSMFeatureExtractor, which adds and manipulates features that will be given to percolator
# https://github.com/OpenMS/OpenMS/blob/342f6524e76a2bab3dcb428ba2f4aa2d6bfe8483/src/topp/PSMFeatureExtractor.cpp
RESCORING_FEATURE_LIST = [
    "isotope_error" "MS:1002049",  # MSGFPlus unchanged RawScore
    "MS:1002050",  # MSGFPlus unchanged DeNovoScore
    "MSGF:ScoreRatio",
    "MSGF:Energy",
    "MSGF:lnEValue",
    "MSGF:lnExplainedIonCurrentRatio",
    "MSGF:lnNTermIonCurrentRatio",
    "MSGF:lnCTermIonCurrentRatio",
    "MSGF:lnMS2IonCurrent",
    "MSGF:MeanErrorTop7",
    "MSGF:sqMeanErrorTop7",
    "MSGF:StdevErrorTop7",
    "XTANDEM:hyperscore",
    "XTANDEM:deltascore",
    "MS:1001330",  # expect_score
    "hyperscore",  # MSfragger
    "nextscore",  # MSfragger
    "COMET:deltaCn",  # recalculated deltaCn = (current_XCorr - 2nd_best_XCorr) / max(current_XCorr, 1)
    "COMET:deltaLCn",  # deltaLCn = (current_XCorr - worst_XCorr) / max(current_XCorr, 1)
    "COMET:lnExpect",  # log(E-value)
    "MS:1002252",  # unchanged XCorr
    "MS:1002255",  # unchanged Sp = number of candidate peptides
    "COMET:lnNumSP",  # log(number of candidate peptides)
    "COMET:lnRankSP",  # log(rank based on Sp score)
    "COMET:IonFrac",  # matched_ions / total_ions
    "MS:1001171",  # unchanged mScore
    "MASCOT:delta_score",  # delta score based on mScore
    "CONCAT:lnEvalue",
    "CONCAT:deltaLnEvalue",
    "SAGE:ln(-poisson)" "SAGE:ln(delta_best)",
    "SAGE:ln(delta_next)",
    "SAGE:ln(matched_intensity_pct)",
    "SAGE:longest_b",
    "SAGE:longest_y",
    "SAGE:longest_y_pct",
    "SAGE:matched_peaks",
    "SAGE:scored_candidates",
]


class IdXMLReader(ReaderBase):
    def __init__(self, filename: Union[Path, str], *args, **kwargs) -> None:
        """
        Reader for idXML files.

        Parameters
        ----------
        filename: str, pathlib.Path
            Path to idXML file.

        Examples
        --------
        >>> from psm_utils.io import IdXMLReader
        >>> reader = IdXMLReader("example.idXML")
        >>> psm_list = [psm for psm in reader]
        """
        super().__init__(filename, *args, **kwargs)
        self.filename = Path(filename)
        protein_ids, peptide_ids = self._parse_idxml()
        self.protein_ids = protein_ids
        self.peptide_ids = peptide_ids
        self.rescoring_features = self._get_rescoring_features(peptide_ids[0].getHits()[0])

    def __iter__(self) -> Iterable[PSM]:
        """
        Iterate over file and return PSMs one-by-one.
        """
        for peptide_id in self.peptide_ids:
            for peptide_hit in peptide_id.getHits():
                yield self._parse_psm(self.protein_ids, peptide_id, peptide_hit)

    def _parse_idxml(self) -> Tuple(oms.ProteinIdentification, oms.PeptideIdentification):
        """
        Parse idXML using pyopenms and do some sanity checks to make sure the file is not empty.
        """
        protein_ids, peptide_ids = [], []
        oms.IdXMLFile().load(str(self.filename), protein_ids, peptide_ids)

        if len(protein_ids) == 0:
            raise IdXMLReaderEmptyListException(
                f"File {self.filename} contains no proteins. Nothing to parse."
            )
        elif len(peptide_ids) == 0:
            raise IdXMLReaderEmptyListException(
                f"File {self.filename} contains no PeptideIdentifications. Nothing to parse."
            )
        elif len(peptide_ids[0].getHits()) == 0:
            raise IdXMLReaderEmptyListException(
                f"File {self.filename} contains no PeptideHits. Nothing to parse."
            )
        else:
            return protein_ids, peptide_ids

    @staticmethod
    def _parse_peptidoform(sequence: str, charge: int) -> str:
        """
        Parse idXML peptide to :py:class:`~psm_utils.peptidoform.Peptidoform`.


        Notes
        -----
        Implemented according to the documentation on
        `github.com/OpenMS/OpenMS <https://github.com/OpenMS/OpenMS/blob/8cb90/src/openms/include/OpenMS/CHEMISTRY/AASequence.h>`_
        . The differentiation between square- and round bracket notation is removed
        after parsing.
        """
        sequence = MOD_PATTERN.sub(r"[\1]", sequence)
        if sequence[:2] == ".[":
            sequence = MOD_PATTERN_NTERM.sub(r"[\1]-", sequence)
        if sequence[-1] == "]":
            sequence = MOD_PATTERN_CTERM.sub(r"-[\1]", sequence)
        sequence = sequence.strip(".")
        sequence += f"/{charge}"

        return sequence

    def _parse_psm(
        self,
        protein_ids: oms.ProteinIdentification,
        peptide_id: oms.PeptideIdentification,
        peptide_hit: oms.PeptideHit,
    ) -> PSM:
        """
        Parse idXML :py:class:`~pyopenms.PeptideHit` to :py:class:`~psm_utils.psm.PSM`.
        Use additional information from :py:class:`~pyopenms.ProteinIdentification` and :py:class:`~pyopenms.PeptideIdentification`
        to annotate parameters of the :py:class:`~psm_utils.psm.PSM` object.
        """
        peptidoform = self._parse_peptidoform(
            peptide_hit.getSequence().toString(), peptide_hit.getCharge()
        )
        return PSM(
            peptidoform=peptidoform,
            spectrum_id=peptide_id.getMetaValue("spectrum_reference"),
            run=self._get_run(protein_ids, peptide_id),
            is_decoy=self._is_decoy(peptide_hit),
            score=peptide_hit.getScore(),
            precursor_mz=peptide_id.getMZ(),
            retention_time=peptide_id.getRT(),
            # NOTE: In future update of OpenMS we will be able to read out ion_mobility from idXML file
            protein_list=[
                accession.decode() for accession in peptide_hit.extractProteinAccessionsSet()
            ],
            rank=peptide_hit.getRank() + 1,  # 0-based to 1-based
            source="idXML",
            # Storing proforma notation of peptidoform and UNIMOD peptide sequence for mapping back to original sequence in writer
            provenance_data={str(peptidoform): peptide_hit.getSequence().toString()},
            # This is needed to calculate a qvalue before rescoring the PSMList
            metadata={
                "idxml:score_type": str(peptide_id.getScoreType()),
                "idxml:higher_score_better": str(peptide_id.isHigherScoreBetter()),
                "idxml:significance_threshold": str(peptide_id.getSignificanceThreshold()),
            },
            rescoring_features={
                key: float(peptide_hit.getMetaValue(key))
                for key in self.rescoring_features
                if self._is_float(peptide_hit.getMetaValue(key))
            },
        )

    @staticmethod
    def _get_run(
        protein_ids: oms.ProteinIdentification, peptide_id: oms.PeptideIdentification
    ) -> str:
        """
        Get run name from idXML using pyopenms
        If idXML contains merge index, use it to annotate the run name without extension
        """
        if peptide_id.metaValueExists("id_merge_index"):
            return Path(
                protein_ids[0]
                .getMetaValue("spectra_data")[peptide_id.getMetaValue("id_merge_index")]
                .decode()
            ).stem
        else:
            return Path(protein_ids[0].getMetaValue("spectra_data")[0].decode()).stem

    @staticmethod
    def _get_rescoring_features(peptide_hit: oms.PeptideHit) -> List[str]:
        """
        Get list of rescoring features from idXML
        """
        # Fill the key list with all the keys from the PeptideHit in the pyopenms way
        keys = []
        peptide_hit.getKeys(keys)
        # Convert from byte to str and remove the keys, which values are not in the fixed list of features
        keys = [key.decode() for key in keys if key.decode() in RESCORING_FEATURE_LIST]

        return keys

    @staticmethod
    def _is_float(element: any) -> bool:
        """
        Check if element is float.
        """
        if element is None:
            return False
        try:
            float(element)
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_decoy(peptide_hit: oms.PeptideHit) -> bool:
        """
        Check if PSM is target or decoy
        """
        if peptide_hit.metaValueExists("target_decoy"):
            return peptide_hit.getMetaValue("target_decoy") == "decoy"
        else:
            return None


class IdXMLReaderEmptyListException(PSMUtilsException):
    """Exception in psm_utils.io.IdXMLReader"""


class IdXMLWriter(WriterBase):
    def __init__(
        self,
        filename: str | Path,
        protein_ids=None,
        peptide_ids=None,
        *args,
        **kwargs,
    ) -> None:
        """
        Writer for idXML files.

        Parameters
        ----------
        filename: str, Pathlib.Path
            Path to PSM file.
        run : str, optional
            Name of the MS run. Usually the spectrum file filename without
            extension.

        Notes
        -----
        - Unlike other psm_utils.io writer classes, :py:class:`IdXMLWriter` does not support writing
          a single PSM to a file with the :py:meth:`write_psm` method. Only writing a full PSMList
          to a file at once with the :py:meth:`write_file` method is currently supported.
        """
        super().__init__(filename, *args, **kwargs)
        self.protein_ids = protein_ids
        self.peptide_ids = peptide_ids
        self._writer = None

    def __enter__(self) -> IdXMLWriter:
        return self

    def __exit__(self, *args, **kwargs) -> None:
        pass

    def write_psm(self, psm: PSM):
        """
        Write a single PSM to the PSM file.

        This method is currently not supported (see Notes).

        Raises
        ------
        NotImplementedError
            OmsIdXMLWriter currently does not support write_psm.

        """
        raise NotImplementedError("IdXMLWriter currently does not support write_psm.")

    def write_file(self, psm_list: PSMList) -> None:
        """
        Write an entire PSMList to the PSM file or update the :py:class:`~pyopenms.ProteinIdentification`
        and :py:class:`~pyopenms.PeptideIdentification` objects with novel features from the PSMList.
        """
        psm_dict = psm_list.get_psm_dict()

        if self.protein_ids is not None and self.peptide_ids is not None:
            self._update_existing_ids(psm_dict)
        # Check if one of self.protein_ids or self.peptide_ids is None
        elif self.protein_ids is not None or self.peptide_ids is not None:
            logger.warning(
                "One of the protein_ids or peptide_ids is None. Falling back to creating new idXML files solely based on the PSMList."
            )
            self._create_new_ids(psm_dict)
        else:
            self._create_new_ids(psm_dict)

    def _update_existing_ids(self, psm_dict: dict) -> None:
        """
        Update existing :py:class:`~pyopenms.ProteinIdentification`
        and :py:class:`~pyopenms.PeptideIdentification` objects with novel features from the PSMList.
        """
        # Access run name(s) from ProteinIdentification
        spectrum_files = [
            Path(run.decode()).stem for run in self.protein_ids[0].getMetaValue("spectra_data")
        ]
        for peptide_id in self.peptide_ids:
            if len(spectrum_files) > 1:
                run = spectrum_files[peptide_id.getMetaValue("id_merge_index")]
            else:
                run = spectrum_files[0]
            # Get PSM objects associated from runs since we are writing a merged idXML
            # NOTE: Collections with multiple protein_ids and peptide_ids is not supported
            try:
                psms = psm_dict[None][run][peptide_id.getMetaValue("spectrum_reference")]
            except KeyError:
                print(
                    "Multiple collections are not supported when parsing single pyopenms protein and peptide objects."
                )
            # Dict of UNIMOD peptide sequence and PSM object
            hit_dict = {psm.provenance_data[str(psm.peptidoform)]: psm for psm in psms}
            # Update PeptideHits according to the PSM objects
            updated_peptide_hits = []
            for peptide_hit in peptide_id.getHits():
                sequence = peptide_hit.getSequence().toString()
                psm = hit_dict[sequence]
                self._update_peptide_hit(peptide_hit, psm)
                updated_peptide_hits.append(peptide_hit)

            peptide_id.setHits(updated_peptide_hits)

        oms.IdXMLFile().store(str(self.filename), self.protein_ids, self.peptide_ids)

    def _update_peptide_hit(self, peptide_hit: oms.PeptideHit, psm: PSM) -> None:
        """
        Inplace update of :py:class:`~pyopenms.PeptideHit` with novel predicted features information
        from :py:class:`~psm_utils.psm.PSM`.
        """
        peptide_hit.setScore(psm.score)
        peptide_hit.setRank(psm.rank - 1)  # 1-based to 0-based
        for feature, value in psm.rescoring_features.items():
            if feature not in RESCORING_FEATURE_LIST:
                peptide_hit.setMetaValue(feature, value)

    def _create_new_ids(self, psm_dict: dict) -> None:
        """
        Create new :py:class:`~pyopenms.ProteinIdentification`
        and :py:class:`~pyopenms.PeptideIdentification` objects with novel features from the PSMList.
        """
        for collection, runs in psm_dict.items():
            self.protein_ids = oms.ProteinIdentification()
            self.peptide_ids = []
            # Set msrun filename with spectra_data meta value
            msrun_reference = [run for run in runs.keys()]
            self.protein_ids.setMetaValue("spectra_data", msrun_reference)
            protein_list = []
            for run, psm_dict_run in runs.items():
                for spectrum_id, psms in psm_dict_run.items():
                    protein_list.append(
                        [accession for psm in psms for accession in psm.protein_list]
                    )

                    # Fill PeptideIdentification object with PeptideHits
                    peptide_id = oms.PeptideIdentification()
                    peptide_id.setMetaValue("spectrum_reference", spectrum_id)
                    peptide_id.setMetaValue("id_merge_index", msrun_reference.index(run))
                    if psms[0].precursor_mz is not None:
                        peptide_id.setMZ(psms[0].precursor_mz)
                    if psms[0].retention_time is not None:
                        peptide_id.setRT(psms[0].retention_time)

                    # Fill PeptideHits object
                    peptide_hits = []
                    for psm in psms:
                        peptide_hit = oms.PeptideHit()
                        peptide_hit.setSequence(
                            oms.AASequence.fromString(
                                self._convert_proforma_to_unimod(psm.peptidoform.sequence)
                            )
                        )
                        peptide_hit.setCharge(psm.peptidoform.precursor_charge)
                        peptide_hit.setMetaValue(
                            "target_decoy",
                            ""
                            if psm.is_decoy is None
                            else ("decoy" if psm.is_decoy else "target"),
                        )
                        if psm.score is not None:
                            peptide_hit.setScore(psm.score)
                        if psm.rank is not None:
                            peptide_hit.setRank(psm.rank - 1)  # 1-based to 0-based
                        self._add_meta_values_from_dict(peptide_hit, psm.metadata)
                        self._add_meta_values_from_dict(peptide_hit, psm.provenance_data)
                        self._add_meta_values_from_dict(peptide_hit, psm.rescoring_features)

                        peptide_hits.append(peptide_hit)

                    peptide_id.setHits(peptide_hits)
                    self.peptide_ids.append(peptide_id)

            # Get unique protein accessions
            protein_list = list(
                set([accession for proteins in protein_list for accession in proteins])
            )
            protein_hits = []
            for accession in protein_list:
                protein_hit = oms.ProteinHit()
                protein_hit.setAccession(accession)
                protein_hits.append(protein_hit)
            self.protein_ids.setHits(protein_hits)

            # Write an idXML file for each collection
            oms.IdXMLFile().store(
                "/".join(filter(None, [collection, str(self.filename)])),
                [self.protein_ids],
                self.peptide_ids,
            )

    def _convert_proforma_to_unimod(self, sequence: str) -> str:
        """
        Convert a peptidoform sequence in proforma notation to UNIMOD notation.
        """
        # Replace square brackets with parentheses
        sequence = sequence.replace("[", "(").replace("]", ")")

        # Check for N-terminal and C-terminal modifications
        if sequence[:2] == "((":
            sequence = sequence.replace("((", ".[", 1).replace(")-", ".", 1)
        if sequence[-2:] == "))":
            sequence = sequence[::-1].replace("))", ".]", 1).replace("-(", ".", 1)[::-1]

        return sequence

    def _add_meta_values_from_dict(self, peptide_hit: oms.PeptideHit, d: dict) -> None:
        """
        Add meta values inplace to :py:class:`~pyopenms.PeptideHit` from a dictionary.
        """
        if d is not None:
            for key, value in d.items():
                peptide_hit.setMetaValue(key, value)
