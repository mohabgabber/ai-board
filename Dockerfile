FROM golang:1.27 AS build
WORKDIR /src
COPY service/go.mod service/go.sum ./
RUN go mod download
COPY service/*.go ./
RUN CGO_ENABLED=0 go build -trimpath -o /ai-board .

FROM scratch
COPY --from=build /ai-board /ai-board
USER 65532:65532
EXPOSE 8080
ENTRYPOINT ["/ai-board"]
CMD ["serve", "--root", "/board", "--listen", "0.0.0.0:8080"]
