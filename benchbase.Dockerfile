# Build-only loader image. 'docker compose build loader' does not load or run a workload.
FROM maven:3.9.9-eclipse-temurin-23 AS build
WORKDIR /src
COPY vendor/benchbase/ .
RUN mvn --batch-mode -Dmaven.test.skip=true -P postgres package && mkdir /dist && tar -xzf target/benchbase-postgres.tgz -C /dist --strip-components=1
FROM eclipse-temurin:25-jre
WORKDIR /bench
COPY --from=build /dist/ ./
COPY configs/benchbase-postgres.xml /bench/config/local.xml
ENTRYPOINT ["java","-jar","benchbase.jar","-b","tpcc,chbenchmark","-c","config/local.xml"]
CMD ["--help"]
