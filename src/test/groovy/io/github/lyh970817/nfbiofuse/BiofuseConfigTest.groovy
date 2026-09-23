package io.github.lyh970817.nfbiofuse

import java.nio.file.Paths

import nextflow.exception.AbortOperationException
import nextflow.util.Duration
import spock.lang.Specification
import spock.lang.Unroll

class BiofuseConfigTest extends Specification {

    static final Map REQUIRED = [vcz: '/data/cohort.vcz', manifest: 'cohort.biofuse.csv', manifestTemplate: 'cohort.template.csv']

    def 'is disabled unless a store is configured'() {
        expect:
        !new BiofuseConfig([:]).enabled
        !new BiofuseConfig(null).enabled
        new BiofuseConfig(REQUIRED).enabled
        !new BiofuseConfig(REQUIRED + [enabled: false]).enabled
        new BiofuseConfig(REQUIRED + [enabled: 'true']).enabled
    }

    @Unroll
    def 'refuses to enable without #missing'() {
        when:
        new BiofuseConfig(REQUIRED.findAll { key, value -> key != missing } + [enabled: true])

        then:
        def e = thrown(AbortOperationException)
        e.message.contains("`biofuse.${missing}` is required")

        where:
        missing << ['vcz', 'manifest', 'manifestTemplate']
    }

    def 'a disabled scope needs nothing'() {
        expect:
        !new BiofuseConfig([enabled: false, identity: 'digest']).enabled
    }

    @Unroll
    def 'rejects #option = #value'() {
        when:
        new BiofuseConfig(REQUIRED + [(option): value])

        then:
        def e = thrown(AbortOperationException)
        e.message.contains("`biofuse.${option}` must be ${expected}")

        where:
        option             | value       | expected
        'autosomesOnly'    | 'yes'       | 'a boolean'
        'enabled'          | 1           | 'a boolean'
        'minAlleles'       | 'two'       | 'an integer'
        'readaheadWorkers' | 2.5         | 'an integer'
        'readinessTimeout' | 'soon'      | 'a duration'
        'vcz'              | ['a', 'b']  | 'a string'
    }

    def 'the minimal command names the store, the template, the manifest and the plugin options'() {
        given:
        def config = new BiofuseConfig(REQUIRED)

        expect:
        config.supervisor == 'nf-biofuse-supervisor'
        config.supervisorCommand(Paths.get('/tmp/ready.json'), 4242) == [
            'nf-biofuse-supervisor', 'serve',
            '--vcz', '/data/cohort.vcz',
            '--manifest-template', 'cohort.template.csv',
            '--manifest', 'cohort.biofuse.csv',
            '--watch-pid', '4242',
            '--ready-file', '/tmp/ready.json',
        ]
    }

    def 'every option set is passed in the supervisor spelling, and only then'() {
        given:
        def config = new BiofuseConfig(REQUIRED + [
            supervisor         : '/env/bin/nf-biofuse-supervisor',
            basename           : 'cohort',
            identity           : 'digest',
            stateRoot          : '/scratch/state',
            recordDir          : '/results/records',
            readinessTimeout   : '2min',
            autosomesOnly      : true,
            minAlleles         : 2,
            regions            : '1:1-1000',
            targets            : '2',
            samples            : 'S1,S2',
            samplesFile        : 'samples.txt',
            forceSamples       : true,
            include            : 'QUAL>30',
            exclude            : 'QUAL<10',
            types              : 'snps',
            excludeTypes       : 'indels',
            sourceRevision     : 'release-3',
            readaheadBufferSize: '64M',
            readaheadWorkers   : 4,
            logLevel           : 'DEBUG',
            keepState          : true,
            verbose            : true,
        ])

        when:
        def argv = config.supervisorCommand(Paths.get('/tmp/ready.json'), 1)

        then:
        argv[0..1] == ['/env/bin/nf-biofuse-supervisor', 'serve']
        pairs(argv) == [
            '--vcz'                  : '/data/cohort.vcz',
            '--manifest-template'    : 'cohort.template.csv',
            '--manifest'             : 'cohort.biofuse.csv',
            '--basename'             : 'cohort',
            '--identity'             : 'digest',
            '--state-root'           : '/scratch/state',
            '--record-dir'           : '/results/records',
            '--readiness-timeout'    : '120.0',
            '--min-alleles'          : '2',
            '--regions'              : '1:1-1000',
            '--targets'              : '2',
            '--samples'              : 'S1,S2',
            '--samples-file'         : 'samples.txt',
            '--include'              : 'QUAL>30',
            '--exclude'              : 'QUAL<10',
            '--types'                : 'snps',
            '--exclude-types'        : 'indels',
            '--source-revision'      : 'release-3',
            '--readahead-buffer-size': '64M',
            '--readahead-workers'    : '4',
            '--log-level'            : 'DEBUG',
            '--watch-pid'            : '1',
            '--ready-file'           : '/tmp/ready.json',
        ]
        argv.findAll { it in ['--autosomes-only', '--force-samples', '--keep-state', '--verbose'] }.size() == 4
    }

    def 'false flags and unset options are left to the supervisor defaults'() {
        when:
        def argv = new BiofuseConfig(REQUIRED + [autosomesOnly: false, forceSamples: 'false']).supervisorCommand(Paths.get('/r'), 1)

        then:
        !argv.contains('--autosomes-only')
        !argv.contains('--force-samples')
        !argv.contains('--identity')
        !argv.contains('--readiness-timeout')
    }

    def 'durations are accepted as Duration values, strings and milliseconds'() {
        expect:
        new BiofuseConfig(REQUIRED + [readinessTimeout: Duration.of('30s')]).readinessTimeout == Duration.of('30s')
        new BiofuseConfig(REQUIRED + [readinessTimeout: '1500ms']).supervisorCommand(Paths.get('/r'), 1).contains('1.5')
        new BiofuseConfig(REQUIRED + [readinessTimeout: 90000]).readinessTimeout == Duration.of('90s')
    }

    /* the option/value pairs of a command, from the third element on */
    private static Map<String, String> pairs(List<String> argv) {
        final result = new LinkedHashMap<String, String>()
        int i = 2
        while( i < argv.size() ) {
            if( i + 1 < argv.size() && !argv[i + 1].startsWith('--') ) {
                result[argv[i]] = argv[i + 1]
                i += 2
            }
            else {
                i += 1
            }
        }
        return result
    }
}
