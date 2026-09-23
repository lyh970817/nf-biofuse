/*
 * Copyright 2026, the nf-biofuse authors
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package io.github.lyh970817.nfbiofuse

import java.nio.file.Path

import groovy.transform.CompileStatic
import nextflow.config.spec.ConfigOption
import nextflow.config.spec.ConfigScope
import nextflow.config.spec.ScopeName
import nextflow.exception.AbortOperationException
import nextflow.script.dsl.Description
import nextflow.util.Duration

/**
 * The {@code biofuse} config scope.
 *
 * Every option except {@code enabled} and {@code supervisor} is handed to the supervisor as the
 * command-line option of the same meaning, and only when it is set: the supervisor owns the defaults
 * and validates the values, so the plugin checks only what it needs itself, that the required options
 * are present and that each value has the type its option takes.
 */
@CompileStatic
@ScopeName('biofuse')
@Description('''
    The `biofuse` scope configures the nf-biofuse plugin, which serves a VCF Zarr store to the run as a
    BioFuse PLINK 1 view: the view is mounted on the host, and the manifest naming it is written, before
    the pipeline validates or stages any input, and the mount is torn down when the run ends.
''')
class BiofuseConfig implements ConfigScope {

    static final String DEFAULT_SUPERVISOR = 'nf-biofuse-supervisor'

    @ConfigOption
    @Description('Enable the plugin. Default: `true` when `vcz` is set, `false` otherwise.')
    final Boolean enabled

    @ConfigOption
    @Description('The VCF Zarr store to serve (a directory, Zarr format 2 or 3).')
    final String vcz

    @ConfigOption
    @Description('Where to write the manifest the pipeline reads, e.g. the file given to `--cohort_manifest`.')
    final String manifest

    @ConfigOption
    @Description('''
        Template of the manifest: the pipeline's own samplesheet with the placeholders `{bed}`, `{bim}`,
        `{fam}`, `{prefix}`, `{mount}`, `{basename}` and `{view_id}` where the mounted view goes.
    ''')
    final String manifestTemplate

    @ConfigOption
    @Description('File stem of the virtual trio. Default: the store name without `.vcz`.')
    final String basename

    @ConfigOption
    @Description('''
        `declared` (default) fills `{view_id}` with the view identity; `digest` leaves it empty so the
        pipeline digests the trio through the mount.
    ''')
    final String identity

    @ConfigOption
    @Description('Where the mountpoint and view state live. Default: `$XDG_RUNTIME_DIR`, else `$TMPDIR`, else `/tmp`.')
    final String stateRoot

    @ConfigOption
    @Description('Where `<basename>.biofuse_view.json` is written. Default: the launch directory.')
    final String recordDir

    @ConfigOption
    @Description('Serve autosomes 1-22 only; required when the store has variants on any other contig.')
    final Boolean autosomesOnly

    @ConfigOption
    @Description('Serve sites with at least this many alleles (vcztools `--min-alleles`).')
    final Integer minAlleles

    @ConfigOption
    @Description('Regions to include (vcztools `--regions`, bcftools `-r` syntax).')
    final String regions

    @ConfigOption
    @Description('Target regions to include (vcztools `--targets`, bcftools `-t` syntax).')
    final String targets

    @ConfigOption
    @Description('Samples to include (vcztools `--samples`, bcftools `-s` syntax).')
    final String samples

    @ConfigOption
    @Description('File of sample names to include (vcztools `--samples-file`); its content enters the identity.')
    final String samplesFile

    @ConfigOption
    @Description('Only warn about unknown samples (vcztools `--force-samples`).')
    final Boolean forceSamples

    @ConfigOption
    @Description('Filter expression selecting variant sites (vcztools `--include`).')
    final String include

    @ConfigOption
    @Description('Filter expression excluding variant sites (vcztools `--exclude`).')
    final String exclude

    @ConfigOption
    @Description('Comma-separated variant types to include (vcztools `--types`).')
    final String types

    @ConfigOption
    @Description('Comma-separated variant types to exclude (vcztools `--exclude-types`).')
    final String excludeTypes

    @ConfigOption
    @Description('Declare the store revision instead of digesting its content, for an externally versioned store.')
    final String sourceRevision

    @ConfigOption
    @Description('Cap on the encoder readahead window, e.g. `64M`. Does not enter the identity.')
    final String readaheadBufferSize

    @ConfigOption
    @Description('Worker threads servicing the readahead pool. Does not enter the identity.')
    final Integer readaheadWorkers

    @ConfigOption
    @Description('Mount daemon log level: `WARNING`, `INFO` or `DEBUG`.')
    final String logLevel

    @ConfigOption
    @Description('How long the supervisor waits for the mounted trio to become readable. Default: `120s`.')
    final Duration readinessTimeout

    @ConfigOption
    @Description('Keep the view state directory and its logs after teardown.')
    final Boolean keepState

    @ConfigOption
    @Description('Log the full view identity, the state directory and the daemon command line.')
    final Boolean verbose

    @ConfigOption
    @Description('The supervisor executable. Default: `nf-biofuse-supervisor` on `PATH`.')
    final String supervisor

    /* required by the config spec */
    BiofuseConfig() {}

    BiofuseConfig(Map opts) {
        final options = opts ?: Collections.emptyMap()
        vcz = string(options, 'vcz')
        final explicit = bool(options, 'enabled')
        enabled = explicit != null ? explicit : vcz != null
        manifest = string(options, 'manifest')
        manifestTemplate = string(options, 'manifestTemplate')
        basename = string(options, 'basename')
        identity = string(options, 'identity')
        stateRoot = string(options, 'stateRoot')
        recordDir = string(options, 'recordDir')
        autosomesOnly = bool(options, 'autosomesOnly')
        minAlleles = integer(options, 'minAlleles')
        regions = string(options, 'regions')
        targets = string(options, 'targets')
        samples = string(options, 'samples')
        samplesFile = string(options, 'samplesFile')
        forceSamples = bool(options, 'forceSamples')
        include = string(options, 'include')
        exclude = string(options, 'exclude')
        types = string(options, 'types')
        excludeTypes = string(options, 'excludeTypes')
        sourceRevision = string(options, 'sourceRevision')
        readaheadBufferSize = string(options, 'readaheadBufferSize')
        readaheadWorkers = integer(options, 'readaheadWorkers')
        logLevel = string(options, 'logLevel')
        readinessTimeout = duration(options, 'readinessTimeout')
        keepState = bool(options, 'keepState')
        verbose = bool(options, 'verbose')
        supervisor = string(options, 'supervisor') ?: DEFAULT_SUPERVISOR
        if( enabled ) {
            for( String name : ['vcz', 'manifest', 'manifestTemplate'] ) {
                if( !options.get(name) )
                    throw new AbortOperationException("nf-biofuse: `biofuse.${name}` is required when the plugin is enabled")
            }
        }
    }

    /**
     * The supervisor command line: {@code serve}, the plugin's own two options, and every option the
     * user set, in the supervisor's spelling.
     */
    List<String> supervisorCommand(Path readyFile, long watchPid) {
        final List<String> argv = [supervisor, 'serve', '--vcz', vcz, '--manifest-template', manifestTemplate, '--manifest', manifest]
        option(argv, '--basename', basename)
        option(argv, '--identity', identity)
        option(argv, '--state-root', stateRoot)
        option(argv, '--record-dir', recordDir)
        option(argv, '--readiness-timeout', readinessTimeout != null ? String.valueOf(readinessTimeout.toMillis() / 1000.0d) : null)
        option(argv, '--min-alleles', minAlleles)
        option(argv, '--regions', regions)
        option(argv, '--targets', targets)
        option(argv, '--samples', samples)
        option(argv, '--samples-file', samplesFile)
        option(argv, '--include', include)
        option(argv, '--exclude', exclude)
        option(argv, '--types', types)
        option(argv, '--exclude-types', excludeTypes)
        option(argv, '--source-revision', sourceRevision)
        option(argv, '--readahead-buffer-size', readaheadBufferSize)
        option(argv, '--readahead-workers', readaheadWorkers)
        option(argv, '--log-level', logLevel)
        flag(argv, '--autosomes-only', autosomesOnly)
        flag(argv, '--force-samples', forceSamples)
        flag(argv, '--keep-state', keepState)
        flag(argv, '--verbose', verbose)
        argv.addAll(['--watch-pid', String.valueOf(watchPid), '--ready-file', readyFile.toString()])
        return argv
    }

    private static void option(List<String> argv, String name, Object value) {
        if( value != null )
            argv.addAll([name, value.toString()])
    }

    private static void flag(List<String> argv, String name, Boolean value) {
        if( value )
            argv.add(name)
    }

    private static String string(Map options, String name) {
        final value = options.get(name)
        if( value == null )
            return null
        if( value instanceof CharSequence || value instanceof Path )
            return value.toString()
        throw invalid(name, value, 'a string')
    }

    private static Boolean bool(Map options, String name) {
        final value = options.get(name)
        if( value == null || value instanceof Boolean )
            return (Boolean) value
        if( value instanceof CharSequence && value.toString() in ['true', 'false'] )
            return value.toString() == 'true'
        throw invalid(name, value, 'a boolean')
    }

    private static Integer integer(Map options, String name) {
        final value = options.get(name)
        if( value == null )
            return null
        if( value instanceof Integer || value instanceof Long || value instanceof Short )
            return ((Number) value).intValue()
        if( value instanceof CharSequence && value.toString().isInteger() )
            return value.toString().toInteger()
        throw invalid(name, value, 'an integer')
    }

    private static Duration duration(Map options, String name) {
        final value = options.get(name)
        if( value == null || value instanceof Duration )
            return (Duration) value
        try {
            if( value instanceof CharSequence )
                return Duration.of(value.toString())
            if( value instanceof Number )
                return Duration.of(((Number) value).longValue())
        }
        catch( IllegalArgumentException e ) {
            // reported below
        }
        throw invalid(name, value, "a duration such as '120s'")
    }

    private static AbortOperationException invalid(String name, Object value, String expected) {
        new AbortOperationException("nf-biofuse: `biofuse.${name}` must be ${expected}, got '${value}'")
    }
}
