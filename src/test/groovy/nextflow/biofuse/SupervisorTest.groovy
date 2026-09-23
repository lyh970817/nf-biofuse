package nextflow.biofuse

import java.nio.file.Files
import java.nio.file.Path
import java.nio.file.attribute.PosixFilePermissions

import nextflow.Session
import nextflow.exception.AbortOperationException
import nextflow.exception.AbortRunException
import spock.lang.Specification
import spock.lang.TempDir
import spock.lang.Timeout

/**
 * The plugin's side of the supervisor protocol, against stand-in supervisors: shell scripts that
 * behave as the real one does at each step (report ready and serve until SIGTERM, report a failure,
 * die silently, never become ready).
 */
@Timeout(60)
class SupervisorTest extends Specification {

    @TempDir
    Path dir

    static final String PARSE_ARGS = '''
        echo "fake supervisor argv: $*"
        ready=""
        while [ "$#" -gt 0 ]; do
            if [ "$1" = --ready-file ]; then ready=$2; fi
            shift
        done
        report() { printf '%s\\n' "$1" > "$ready.tmp" && mv "$ready.tmp" "$ready"; }
    '''

    static final String READY_JSON = '{"status":"ready","manifest":"/work/cohort.biofuse.csv","mount":"/state/nf-biofuse/view-0123456789abcdef/mnt","genotype_view_id":"biofuse:ab","identity_mode":"declared","readiness_seconds":0.123,"record":"/work/cohort.biofuse_view.json"}'

    static final Map<String, String> FAKES = [
        ready      : PARSE_ARGS + """
            trap 'echo "fake supervisor: tearing down"; rm -f "\$ready"; exit 0' TERM
            report '${READY_JSON}'
            while true; do sleep 0.05; done
        """,
        failed     : PARSE_ARGS + '''
            echo "fake supervisor: the store is not a Zarr group"
            report '{"status":"failed","exit_code":2,"error":"/data/cohort.vcz is not a Zarr v2 or v3 group root"}'
            exit 2
        ''',
        silent     : PARSE_ARGS + '''
            echo "fake supervisor: Traceback (most recent call last): boom"
            exit 3
        ''',
        neverReady : PARSE_ARGS + '''
            trap 'echo "fake supervisor: stopped before ready"; exit 0' TERM
            while true; do sleep 0.05; done
        ''',
    ]

    private BiofuseConfig config(String fake) {
        final script = dir.resolve("fake-${fake}.sh")
        Files.writeString(script, "#!/usr/bin/env bash\n" + FAKES[fake].stripIndent())
        Files.setPosixFilePermissions(script, PosixFilePermissions.fromString('rwx------'))
        new BiofuseConfig([vcz: '/data/cohort.vcz', manifest: 'cohort.biofuse.csv', manifestTemplate: 't.csv', supervisor: script.toString()])
    }

    def 'waits for the ready report, returns it, and stops the supervisor with SIGTERM'() {
        given:
        def supervisor = new Supervisor(config('ready'), dir, 4242)

        when:
        supervisor.start()
        def ready = supervisor.awaitReady()

        then:
        ready.status == 'ready'
        ready.manifest == '/work/cohort.biofuse.csv'
        ready.genotype_view_id == 'biofuse:ab'

        when:
        supervisor.stop()

        then:
        supervisor.exitStatus == 0
        !Files.exists(supervisor.readyFile)
        Files.readString(supervisor.logFile).contains('fake supervisor: tearing down')
        Files.readString(supervisor.logFile).contains("--watch-pid 4242 --ready-file ${supervisor.readyFile}")

        when: 'stopping again is a no-op'
        supervisor.stop()

        then:
        supervisor.exitStatus == 0
    }

    def 'writes the supervisor log in the launch directory'() {
        expect:
        new Supervisor(config('ready'), dir, 1).logFile == dir.resolve('.nf-biofuse.log')
    }

    def 'a failure the supervisor reports is raised with its message and the log tail'() {
        given:
        def supervisor = new Supervisor(config('failed'), dir, 1)

        when:
        supervisor.start()
        supervisor.awaitReady()

        then:
        def e = thrown(AbortOperationException)
        e.message.contains('the supervisor failed with status 2: /data/cohort.vcz is not a Zarr v2 or v3 group root')
        e.message.contains('fake supervisor: the store is not a Zarr group')

        cleanup:
        supervisor.stop()
    }

    def 'a supervisor that dies without a report is raised with its status and the log tail'() {
        given:
        def supervisor = new Supervisor(config('silent'), dir, 1)

        when:
        supervisor.start()
        supervisor.awaitReady()

        then:
        def e = thrown(AbortOperationException)
        e.message.contains('the supervisor exited with status 3 before the view was ready')
        e.message.contains('Traceback (most recent call last): boom')

        cleanup:
        supervisor.stop()
    }

    def 'a supervisor that cannot be started names the option that fixes it'() {
        given:
        def config = new BiofuseConfig([vcz: 'x.vcz', manifest: 'm', manifestTemplate: 't', supervisor: dir.resolve('missing').toString()])
        def supervisor = new Supervisor(config, dir, 1)

        when:
        supervisor.start()

        then:
        def e = thrown(AbortOperationException)
        e.message.contains('cannot start the supervisor')
        e.message.contains('biofuse.supervisor')
    }

    def 'stopping while it waits for readiness ends the wait and the supervisor'() {
        given:
        def supervisor = new Supervisor(config('neverReady'), dir, 1)
        supervisor.start()
        def stopper = Thread.start { sleep 500; supervisor.stop() }

        when:
        supervisor.awaitReady()

        then:
        def e = thrown(AbortOperationException)
        e.message.contains('the run ended before the view was ready')

        when:
        stopper.join()

        then:
        supervisor.exitStatus == 0
        Files.readString(supervisor.logFile).contains('fake supervisor: stopped before ready')
    }

    def 'the observer blocks onFlowCreate until ready and stops the supervisor at completion'() {
        given:
        def observer = new BiofuseObserver(config('ready'), dir)

        when:
        observer.onFlowCreate(Mock(Session))

        then:
        observer.supervisor.exitStatus == null

        when:
        observer.onFlowComplete()

        then:
        observer.supervisor.exitStatus == 0
    }

    def 'the observer turns a supervisor failure into an AbortRunException, which is what ends a run'() {
        given:
        def observer = new BiofuseObserver(config('failed'), dir)

        when:
        observer.onFlowCreate(Mock(Session))

        then:
        def e = thrown(AbortRunException)
        e.message.contains('the supervisor failed with status 2')
    }

    def 'the factory creates the observer only when the scope enables the plugin'() {
        given:
        def factory = new BiofuseObserverFactory()
        def none = Mock(Session) { getConfig() >> [:] }
        def disabled = Mock(Session) { getConfig() >> [biofuse: [enabled: false, vcz: 'x']] }
        def enabled = Mock(Session) { getConfig() >> [biofuse: [vcz: 'x', manifest: 'm', manifestTemplate: 't']] }

        expect:
        factory.create(none).isEmpty()
        factory.create(disabled).isEmpty()
        factory.create(enabled).collect { it.class } == [BiofuseObserver]
    }

    def 'the factory rejects an incomplete scope before anything starts'() {
        given:
        def incomplete = Mock(Session) { getConfig() >> [biofuse: [vcz: 'x']] }

        when:
        new BiofuseObserverFactory().create(incomplete)

        then:
        def e = thrown(AbortOperationException)
        e.message.contains('`biofuse.manifest` is required')
    }
}
